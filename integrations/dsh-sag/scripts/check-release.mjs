import { readFile } from 'node:fs/promises'
import { resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

/** Keep plugin tags, package identity and source metadata independent from SAG releases. */
export function validateRelease(tag, manifest) {
  if (manifest.name !== '@zleap-ai/dsh-sag') throw new Error('Unexpected npm package name')
  if (!/^\d+\.\d+\.\d+$/u.test(manifest.version)) {
    throw new Error('This workflow publishes stable plugin versions only')
  }
  const expected = `dsh-sag-v${manifest.version}`
  if (tag !== expected) throw new Error(`Plugin release tag must be ${expected}`)
  if (manifest.repository?.url !== 'git+https://github.com/Zleap-AI/SAG.git'
    || manifest.repository?.directory !== 'integrations/dsh-sag/packages/dsh-sag') {
    throw new Error('Plugin repository metadata must identify its SAG source directory')
  }
  return manifest.version
}

if (process.argv[1] !== undefined && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const manifest = JSON.parse(await readFile(new URL('../packages/dsh-sag/package.json', import.meta.url), 'utf8'))
  const tag = process.argv[2] ?? `dsh-sag-v${manifest.version}`
  console.log(`Validated ${tag} (${validateRelease(tag, manifest)})`)
}
