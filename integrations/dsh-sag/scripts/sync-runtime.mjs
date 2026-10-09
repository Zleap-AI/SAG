import { cp, mkdir, rm } from 'node:fs/promises'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const source = resolve(root, 'python/runtime')
const destination = resolve(root, 'packages/dsh-sag/runtime')
const setupDestination = resolve(root, 'packages/dsh-sag/scripts/setup-runtime.mjs')

await rm(destination, { recursive: true, force: true })
await mkdir(destination, { recursive: true })
await cp(resolve(source, 'pyproject.toml'), resolve(destination, 'pyproject.toml'))
await cp(resolve(source, 'uv.lock'), resolve(destination, 'uv.lock'))
await cp(resolve(source, 'src'), resolve(destination, 'src'), {
  recursive: true,
  filter: path => !path.includes('__pycache__') && !path.endsWith('.pyc'),
})
await mkdir(dirname(setupDestination), { recursive: true })
await cp(resolve(root, 'scripts/setup-runtime.mjs'), setupDestination)
