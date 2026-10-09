import { execFileSync } from 'node:child_process'
import { mkdir, readFile, stat } from 'node:fs/promises'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'

const root = fileURLToPath(new URL('..', import.meta.url))
const packageDir = join(root, 'packages', 'dsh-sag')
const destination = join(root, 'artifacts')
const manifest = JSON.parse(await readFile(join(packageDir, 'package.json'), 'utf8'))
execFileSync('pnpm', ['run', 'build'], { cwd: root, stdio: 'inherit' })
await mkdir(destination, { recursive: true })
execFileSync('pnpm', ['--dir', packageDir, 'pack', '--pack-destination', destination], { stdio: 'inherit' })
const archive = join(destination, `zleap-ai-dsh-sag-${manifest.version}.tgz`)
await stat(archive)
console.log(`Install with: dsh plugin --profile web add ${archive}`)
