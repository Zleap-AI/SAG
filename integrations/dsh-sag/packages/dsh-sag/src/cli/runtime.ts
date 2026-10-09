import { readFile } from 'node:fs/promises'
import { homedir } from 'node:os'
import { resolve } from 'node:path'
import { Context } from '@deepseek-ai/cordis'
import { LocalCredentialProvider } from '@deepseek-ai/dsh-credentials-local'
import { profileConnectionSettings, type CliProfileOptions } from './profile-settings.js'
import { resolveConfig } from '../config.js'
import { discoverConnection, platformConnectionPaths, type DiscoverConnectionOptions } from '../connection/discovery.js'
import type { SagConnectionManagerDeps } from '../connection/manager.js'
import { SagConnectionStore } from '../connection/store.js'

/** Candidate locations used by one non-interactive connection discovery attempt. */
export interface CliDiscoveryOptions {
  readonly paths?: readonly string[]
  readonly urls?: readonly string[]
}

/** The live local services consumed by the command-line interface. */
export interface CliRuntime {
  readonly store: SagConnectionStore
  readonly discovery: {
    discover(signal: AbortSignal, options?: CliDiscoveryOptions): ReturnType<typeof discoverConnection>
  }
  readonly inspect?: SagConnectionManagerDeps['inspect']
  dispose(): Promise<void>
}

/** One initialized Cordis layer that can release its owned resources. */
export interface CliRuntimeLayer {
  dispose(): Promise<void>
}

/** Layer startup operations for the short-lived CLI runtime. */
export interface CliRuntimeLayerFactory {
  startSettings(): Promise<CliRuntimeLayer>
  startCredentials(): Promise<CliRuntimeLayer>
  startRegistration(): Promise<{ readonly layer: CliRuntimeLayer; readonly store: SagConnectionStore }>
}

/** Settings store and reverse-order disposer created from initialized runtime layers. */
export interface CliRuntimeLifecycle {
  readonly store: SagConnectionStore
  dispose(): Promise<void>
}

function runtimeFileSystem(): DiscoverConnectionOptions['fs'] {
  return {
    async resolve(path) {
      const filename = resolve(path)
      return { targetKey: filename as never, displayPath: filename }
    },
    async readText(target, signal) {
      if (signal?.aborted) throw signal.reason
      return readFile(target.displayPath, 'utf8')
    },
  }
}

async function disposalFailures(layers: readonly CliRuntimeLayer[]): Promise<unknown[]> {
  const failures: unknown[] = []
  for (const layer of [...layers].reverse()) {
    try {
      await layer.dispose()
    } catch (error) {
      failures.push(error)
    }
  }
  return failures
}

function throwDisposalFailures(failures: readonly unknown[]): never | void {
  if (failures.length === 0) return
  if (failures.length === 1) throw failures[0]
  throw new AggregateError(failures, 'dsh-sag: multiple CLI runtime disposal failures')
}

/** Start nested runtime layers and guarantee every initialized layer is released in reverse order. */
export async function startCliRuntime(factory: CliRuntimeLayerFactory): Promise<CliRuntimeLifecycle> {
  const layers: CliRuntimeLayer[] = []
  let store: SagConnectionStore
  try {
    layers.push(await factory.startSettings())
    layers.push(await factory.startCredentials())
    const registration = await factory.startRegistration()
    layers.push(registration.layer)
    store = registration.store
  } catch (error) {
    const failures = await disposalFailures(layers)
    if (failures.length === 0) throw error
    throw new AggregateError([error, ...failures], 'dsh-sag: CLI runtime startup and disposal failed')
  }
  return {
    store: store!,
    async dispose() {
      throwDisposalFailures(await disposalFailures(layers))
    },
  }
}

async function activatedLayer(factory: () => CliRuntimeLayer & PromiseLike<CliRuntimeLayer>): Promise<CliRuntimeLayer> {
  const layer = factory()
  try {
    await layer
    return layer
  } catch (error) {
    const failures = await disposalFailures([layer])
    if (failures.length === 0) throw error
    throw new AggregateError([error, ...failures], 'dsh-sag: CLI runtime layer startup and disposal failed')
  }
}

/** Start short-lived local settings and credential providers under the shared DSH home. */
export async function createCliRuntime(options: CliProfileOptions = {}): Promise<CliRuntime> {
  const ctx = new Context()
  const dshHome = options.dshHome ?? process.env.DSH_HOME
  const providerConfig = { watch: false, ...(dshHome === undefined ? {} : { dshHome }) }
  const lifecycle = await startCliRuntime({
    startSettings: () => Promise.resolve({ dispose: () => Promise.resolve() }),
    startCredentials: () => activatedLayer(() => ctx.plugin(LocalCredentialProvider, providerConfig)),
    async startRegistration() {
      let store: SagConnectionStore | undefined
      const layer = await activatedLayer(() => ctx.plugin({
        inject: ['credentials'],
        apply(child: Context) {
          store = new SagConnectionStore({ credentials: child.credentials, settings: profileConnectionSettings(options) })
        },
      }))
      if (store === undefined) {
        await layer.dispose()
        throw new Error('dsh-sag: local settings runtime did not start')
      }
      return { layer, store }
    },
  })

  const fs = runtimeFileSystem()
  const defaults = resolveConfig({})
  if (defaults.mode !== 'local') throw new Error('dsh-sag: local discovery defaults are unavailable')
  return {
    store: lifecycle.store,
    discovery: {
      discover(signal, options = {}) {
        return discoverConnection({
          fs,
          paths: options.paths ?? platformConnectionPaths(process.env, process.platform, homedir()),
          urls: options.urls ?? defaults.discoveryUrls,
          fetch: (url, init) => fetch(url, init),
        }, signal)
      },
    },
    dispose: lifecycle.dispose,
  }
}
