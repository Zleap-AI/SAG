import { build } from 'esbuild'
import { readFile, readdir, writeFile } from 'node:fs/promises'
import { join, resolve, sep } from 'node:path'
import { fileURLToPath } from 'node:url'

const packageDir = fileURLToPath(new URL('../packages/dsh-sag/', import.meta.url))
const cliEntry = join(packageDir, 'lib', 'cli.js')
const cliBundle = join(packageDir, 'lib', 'dsh-sag-cli.js')
const noticesFile = join(packageDir, 'THIRD_PARTY_NOTICES')

function bundledPackageRoot(input) {
  const absolute = resolve(input)
  const marker = `${sep}node_modules${sep}`
  const boundary = absolute.lastIndexOf(marker)
  if (boundary < 0) return undefined
  const parent = absolute.slice(0, boundary + marker.length)
  const segments = absolute.slice(boundary + marker.length).split(sep)
  const packageSegments = segments[0]?.startsWith('@') ? segments.slice(0, 2) : segments.slice(0, 1)
  return packageSegments.length === 0 ? undefined : join(parent, ...packageSegments)
}

function sourceReference(manifest) {
  const repository = manifest.repository
  if (typeof repository === 'string') return repository
  if (repository !== null && typeof repository === 'object' && typeof repository.url === 'string') {
    return repository.directory === undefined
      ? repository.url
      : `${repository.url} (directory: ${String(repository.directory)})`
  }
  if (typeof manifest.homepage === 'string') return manifest.homepage
  throw new Error(`bundled package ${String(manifest.name)} has no repository or homepage source`)
}

async function generateNotices(inputs) {
  const roots = [...new Set(inputs.map(bundledPackageRoot).filter(root => root !== undefined))]
  const packages = []
  for (const root of roots) {
    const manifest = JSON.parse(await readFile(join(root, 'package.json'), 'utf8'))
    const licenseFiles = (await readdir(root)).filter(name => /^(?:licen[cs]e|copying|notice)(?:\.|$)/iu.test(name)).sort()
    if (typeof manifest.name !== 'string' || typeof manifest.version !== 'string' || manifest.license === undefined) {
      throw new Error(`bundled package manifest is missing name, version, or license: ${root}`)
    }
    if (licenseFiles.length === 0) throw new Error(`bundled package ${manifest.name} ships no license file`)
    packages.push({ manifest, root, licenseFiles })
  }
  packages.sort((left, right) => `${left.manifest.name}@${left.manifest.version}`.localeCompare(`${right.manifest.name}@${right.manifest.version}`, 'en'))

  const sections = ['# Third-Party Notices', '', 'Generated from the package manifests and license files used by the standalone dsh-sag CLI build.', '']
  for (const item of packages) {
    sections.push(`## ${item.manifest.name}@${item.manifest.version}`)
    sections.push('')
    sections.push(`Source: ${sourceReference(item.manifest)}`)
    sections.push(`License: ${typeof item.manifest.license === 'string' ? item.manifest.license : JSON.stringify(item.manifest.license)}`)
    for (const filename of item.licenseFiles) {
      sections.push(`License file: ${filename}`)
      sections.push('')
      sections.push((await readFile(join(item.root, filename), 'utf8')).trimEnd().replace(/[ \t]+$/gmu, ''))
      sections.push('')
    }
  }
  await writeFile(noticesFile, `${sections.join('\n').trimEnd()}\n`)
}

const result = await build({
  entryPoints: [cliEntry],
  outfile: cliBundle,
  bundle: true,
  platform: 'node',
  format: 'esm',
  target: 'node22',
  sourcemap: false,
  legalComments: 'eof',
  metafile: true,
  banner: {
    js: "import { createRequire as dshSagCreateRequire } from 'node:module'; const require = dshSagCreateRequire(import.meta.url);",
  },
})

if (result.metafile === undefined) throw new Error('esbuild returned no CLI dependency metadata')
await generateNotices(Object.keys(result.metafile.inputs))
