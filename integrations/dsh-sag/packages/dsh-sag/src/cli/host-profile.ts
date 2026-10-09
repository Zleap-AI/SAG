import { existsSync, readFileSync, realpathSync } from 'node:fs'
import { createRequire } from 'node:module'
import { basename, delimiter, isAbsolute, join, resolve } from 'node:path'
import {
  bundlePatchPaths, evaluatePluginCompatibility, loadOptionalPatches, loadOverlayPatches,
  pluginCompatibilityWarning, readProfileManifest, readProfileVersionExemptions, resolveBundleDir,
  type Profile,
} from '@deepseek-ai/dsh-app-boot'

/** The actual installed host package; independent plugin versions never stand in for it. */
export interface CliHostInstallation {
  readonly anchor: string
  readonly version: string
}

/** Resolve the host from the selected profile, or its real CLI on PATH/DSH_BIN. */
export function resolveCliHostInstallation(dir: string, env: NodeJS.ProcessEnv = process.env): CliHostInstallation {
  const from = (anchor: string): CliHostInstallation => {
    const filename = createRequire(anchor).resolve('@deepseek-ai/dsh/package.json')
    const manifest = JSON.parse(readFileSync(filename, 'utf8'))
    if (typeof manifest.version !== 'string' || !manifest.version.trim()) throw new Error('installed dsh package has no exact version')
    if (manifest.name !== '@deepseek-ai/dsh') throw new Error('unexpected host package identity')
    // The official evaluator validates exact semantic-version spelling too.
    evaluatePluginCompatibility({}, {}, manifest.version)
    return { anchor: filename, version: manifest.version }
  }
  const binaries = (name: string): string[] => {
    if (isAbsolute(name) || name.includes('/') || name.includes('\\')) return [resolve(name)]
    return (env.PATH ?? '').split(delimiter).filter(Boolean).map(path => join(path, name))
  }
  if (env.DSH_BIN?.trim()) {
    for (const binary of binaries(env.DSH_BIN)) {
      if (existsSync(binary)) return from(realpathSync(binary))
    }
    throw new Error('dsh-sag: DSH_BIN does not locate an installed dsh CLI')
  }
  try { return from(join(dir, 'package.json')) } catch { /* 0.2 may supply host modules only during boot. */ }
  for (const binary of binaries(process.platform === 'win32' ? 'dsh.cmd' : 'dsh')) {
    if (!existsSync(binary)) continue
    try { return from(realpathSync(binary)) } catch { /* Keep looking for the installed CLI package. */ }
  }
  throw new Error('dsh-sag: cannot resolve the installed dsh package; put its CLI on PATH or set DSH_BIN to the dsh executable used for this profile')
}

/** Load official patch layers with the resolved host's exact version, including in a standalone bundle. */
export function loadCliProfile(dir: string): Profile {
  const host = resolveCliHostInstallation(dir)
  const bundles = readProfileManifest('dsh-sag', dir).dsh?.profile?.bundles ?? []
  const exemptions = readProfileVersionExemptions(dir)
  const layers: Profile['layers'] = []
  const skippedBundles: Profile['skippedBundles'] = []
  for (const packageName of bundles) {
    try {
      const packageDir = resolveBundleDir('dsh-sag', packageName, host.anchor, dir)
      const manifest = readProfileManifest('dsh-sag', packageDir)
      if (!manifest.dsh?.bundle) throw new Error(`profile bundle ${packageName} declares no dsh.bundle`)
      const issue = evaluatePluginCompatibility(manifest, exemptions, host.version)
      if (issue && !issue.exempted) throw new Error(pluginCompatibilityWarning(issue))
      const patchPaths = bundlePatchPaths(packageDir, manifest.dsh.bundle)
      layers.push({ packageName, packageDir, patchPaths, patches: patchPaths.flatMap(path => loadOverlayPatches('dsh-sag', path)) })
    } catch (error) { skippedBundles.push({ packageName, reason: String(error) }) }
  }
  const patchPath = join(dir, 'cordis.patch.yml')
  return { name: basename(dir), dir, layers, patchPath, patches: loadOptionalPatches('dsh-sag', patchPath) ?? [], skippedBundles }
}
