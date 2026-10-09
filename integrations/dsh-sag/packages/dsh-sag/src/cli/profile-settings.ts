import { readFile } from 'node:fs/promises'
import { join, resolve } from 'node:path'
import { composeEntries, loadOptionalPatches } from '@deepseek-ai/dsh-app-boot'
import { writeFileAtomic } from '@deepseek-ai/dsh-atomic-write'
import { resolveDshHome } from '@deepseek-ai/dsh-home-paths'
import { Scalar, isMap, isSeq, parseDocument, visit } from 'yaml'
import { withCliProfileLock } from './profile-lock.js'
import { loadCliProfile } from './host-profile.js'
import { DEFAULT_CONNECTION_SETTINGS } from '../config.js'
import type { SagConnectionSettings } from '../connection/store.js'
import type { SagLocalSettings } from '../connection/types.js'

/** Profile chosen by `dsh plugin --profile … exec`; pnpm executes in that directory. */
export interface CliProfileOptions {
  readonly dshHome?: string
  readonly profileDir?: string
}

function object(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
}

/** Edit only the connection field, using the same profile lock as the host's ConfigEditor. */
export function profileConnectionSettings(options: CliProfileOptions = {}): SagConnectionSettings {
  const dir = resolve(options.profileDir ?? process.cwd())
  const home = resolveDshHome(options.dshHome)
  const filename = join(dir, 'cordis.patch.yml')

  function config(patches?: ReturnType<typeof loadCliProfile>['patches']): Record<string, unknown> {
    const profile = loadCliProfile(dir)
    const rows = composeEntries([
      ...profile.layers.map(layer => layer.patches), patches ?? profile.patches,
      loadOptionalPatches('dsh-sag', join(home, 'cordis.patch.yml')) ?? [],
    ])
    const matches = rows.filter(row => row.id === 'dsh-sag')
    if (matches.length !== 1) {
      throw new Error(`dsh-sag: run this command with dsh plugin --profile <name> exec dsh-sag in a profile containing dsh-sag${profile.skippedBundles.length ? `; ${profile.skippedBundles.map(bundle => bundle.reason).join('; ')}` : ''}`)
    }
    if (!object(matches[0]?.config)) return {}
    return matches[0].config
  }

  async function write(connection: object): Promise<void> {
    await withCliProfileLock(dir, async () => {
      const current = config()
      if (current.mode === 'embedded') {
        throw new Error('dsh-sag: setup cannot replace the connection in an embedded profile; select a local profile')
      }
      let before: string
      try { before = await readFile(filename, 'utf8') } catch (error) {
        if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw error
        before = '[]\n'
      }
      const document = parseDocument(before, { customTags: [{ tag: 'tag:yaml.org,2002:js', resolve: (value: string) => value }] })
      if (document.errors[0]) throw document.errors[0]
      if (!isSeq(document.contents)) throw new Error('dsh-sag: profile patch must be a YAML sequence')
      let index = -1
      document.contents.items.forEach((item, position) => {
        if (isMap(item) && document.getIn([position, 'id']) === 'dsh-sag' && !item.has('insert')) index = position
      })
      if (index < 0) document.add(document.createNode({ id: 'dsh-sag', config: { ...current, connection } }))
      else {
        for (const [key, value] of Object.entries(current)) {
          if (key !== 'connection' && !document.hasIn([index, 'config', key])) {
            document.setIn([index, 'config', key], document.createNode(value))
          }
        }
        document.setIn([index, 'config', 'connection'], document.createNode(connection))
      }
      visit(document, { Map(_key, node) {
        if (node.items.length !== 1 || typeof node.get('__jsExpr') !== 'string') return
        const expression = new Scalar(node.get('__jsExpr'))
        expression.tag = 'tag:yaml.org,2002:js'
        return expression
      } })
      document.contents.flow = false
      // Profile patches merge config values. Reuse the host parser for expressions,
      // and refuse writes that a home override would immediately shadow.
      const profile = loadCliProfile(dir)
      const nextPatches = [...profile.patches]
      let patchIndex = -1
      nextPatches.forEach((row, position) => {
        if (row.id === 'dsh-sag' && row.insert === undefined) patchIndex = position
      })
      if (patchIndex < 0) nextPatches.push({ id: 'dsh-sag', config: { ...current, connection } })
      else nextPatches[patchIndex] = {
        ...nextPatches[patchIndex], config: { ...current, ...nextPatches[patchIndex]!.config, connection },
      }
      if (JSON.stringify(config(nextPatches).connection) !== JSON.stringify(connection)) {
        throw new Error('dsh-sag: connection is overridden by a home patch; edit that override before setup')
      }
      await writeFileAtomic(filename, String(document), { mode: 0o600 })
    })
  }

  return {
    get() {
      const current = config().connection
      return { ...DEFAULT_CONNECTION_SETTINGS, ...(object(current) ? current : {}) } as SagLocalSettings
    },
    update: write,
    replace: next => write({ ...DEFAULT_CONNECTION_SETTINGS, ...next }),
  }
}
