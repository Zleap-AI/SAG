import type { SubprocessHandle, SubprocessRuntime } from '@deepseek-ai/dsh-subprocess'
import { SagRuntimeClient } from './client.js'
import {
  REQUIRED_ENGINE_VERSION,
  RPC_PROTOCOL_VERSION,
  type InitializeResult,
  type RpcResult,
} from './protocol.js'

export interface SupervisorConfig {
  readonly pythonCommand: string
  readonly envFile: string
  readonly cwd: string
  readonly namespaces: readonly { readonly id: string; readonly label: string }[]
  readonly maxReadEngines: number
  readonly maxResults: number
  readonly maxSnippetChars: number
  readonly maxReadChars: number
  readonly shutdownGraceMs: number
  readonly allowDegraded: boolean
}

type RuntimeLauncher = Pick<SubprocessRuntime, 'resolveExecutable' | 'spawn'>

function initializeResult(result: RpcResult): InitializeResult {
  if (!('protocolVersion' in result) || !('engineVersion' in result) || !('namespaces' in result)) {
    throw new Error('SAG runtime initialize returned the wrong result type')
  }
  return result as unknown as InitializeResult
}

/** Owns one Python sidecar from executable resolution through tree quiescence. */
export class SagRuntimeSupervisor {
  readonly client: SagRuntimeClient
  readonly #handle: SubprocessHandle
  readonly #graceMs: number
  #disposed = false

  private constructor(handle: SubprocessHandle, client: SagRuntimeClient, graceMs: number) {
    this.#handle = handle
    this.client = client
    this.#graceMs = graceMs
  }

  /** Spawn and verify a sidecar before exposing its client to tool registration. */
  static async start(runtime: RuntimeLauncher, config: SupervisorConfig): Promise<SagRuntimeSupervisor> {
    const python = await runtime.resolveExecutable(config.pythonCommand, { PYTHONUNBUFFERED: '1' })
    const namespaceArgs = config.namespaces.flatMap(namespace => ['--namespace', namespace.id])
    const handle = runtime.spawn({
      argv: [
        python,
        '-m',
        'dsh_sag_runtime',
        '--env-file',
        config.envFile,
        ...namespaceArgs,
        '--max-read-engines',
        String(config.maxReadEngines),
        '--max-results',
        String(config.maxResults),
        '--max-excerpt-chars',
        String(config.maxSnippetChars),
        '--max-read-chars',
        String(config.maxReadChars),
      ],
      cwd: config.cwd,
      stdio: {
        stdin: 'pipe',
        stdout: 'pipe',
        stderr: { maxBytes: 64 * 1024 },
      },
      graceMs: config.shutdownGraceMs,
      env: { PYTHONUNBUFFERED: '1' },
    })
    if (!handle.stdin || !handle.stdout) {
      handle.terminate()
      throw new Error('SAG runtime did not expose piped stdin/stdout')
    }
    const client = new SagRuntimeClient(handle.stdin, handle.stdout, handle.done)
    const supervisor = new SagRuntimeSupervisor(handle, client, config.shutdownGraceMs)
    try {
      const ready = initializeResult(await client.request('initialize', { protocolVersion: RPC_PROTOCOL_VERSION }))
      if (ready.protocolVersion !== RPC_PROTOCOL_VERSION) {
        throw new Error(`SAG runtime protocol ${RPC_PROTOCOL_VERSION} is required`)
      }
      if (ready.engineVersion !== REQUIRED_ENGINE_VERSION) {
        throw new Error(`zleap-sag ${REQUIRED_ENGINE_VERSION} is required; sidecar reported ${ready.engineVersion}`)
      }
      if (!ready.evidenceRead) throw new Error('SAG runtime does not support evidence reads')
      const expectedNamespaces = config.namespaces.map(namespace => namespace.id)
      if (JSON.stringify(ready.namespaces) !== JSON.stringify(expectedNamespaces)) {
        throw new Error('SAG runtime namespace handshake does not match plugin configuration')
      }
      if (ready.health === 'unavailable' || (ready.health === 'degraded' && !config.allowDegraded)) {
        throw new Error(`SAG runtime health is ${ready.health}`)
      }
      return supervisor
    } catch (error) {
      handle.terminate()
      client.closeInput()
      await handle.waitForExit()
      throw error
    }
  }

  /** Gracefully stop admission, then escalate through the managed process seam. */
  async dispose(): Promise<void> {
    if (this.#disposed) return
    this.#disposed = true
    let timer: ReturnType<typeof setTimeout> | undefined
    const timeout = new Promise<'timeout'>(resolve => {
      timer = setTimeout(() => resolve('timeout'), this.#graceMs)
    })
    const shutdown = this.client.request('shutdown', {}).then(() => 'shutdown' as const)
    const outcome = await Promise.race([shutdown, timeout]).catch(() => 'failure' as const)
    if (timer) clearTimeout(timer)
    this.client.closeInput()
    if (outcome !== 'shutdown') this.#handle.terminate()
    const signal = AbortSignal.timeout(this.#graceMs)
    const exited = await this.#handle.waitForExit(signal)
    if (!exited) {
      this.#handle.terminate()
      await this.#handle.waitForExit()
    }
    await this.#handle.done
  }
}
