import type { DiscoveryResult } from './discovery.js'
import type { SagCapabilityDescriptor, SagConnectionDescriptor } from './types.js'
import { SagApiClient, SagApiError } from '../local/api-client.js'
import { createSagGateway, type SagGateway } from '../local/gateway.js'
import {
  McpProbeIncompatibleError,
  probeMcp,
  type McpProbeResult,
} from '../local/mcp-probe.js'

/** The only connection states exposed to setup, doctor, and tools. */
export type SagConnectionStatus = 'ready' | 'not-found' | 'unreachable' | 'incompatible'

/** Non-mutating checks made against one candidate connection. */
export interface ConnectionInspection {
  readonly status: SagConnectionStatus
  readonly health: boolean
  readonly ready: boolean
  readonly capabilities?: SagCapabilityDescriptor
  readonly sourceCount: number
  readonly mcp?: McpProbeResult
  readonly errors?: readonly string[]
}

/** A manager result suitable for tools and human-readable doctor output. */
export interface SagConnectionReport extends ConnectionInspection {
  readonly descriptor?: SagConnectionDescriptor
  readonly gateway?: SagGateway
  readonly discovery?: DiscoveryResult
}

/** Read access to the explicitly saved connection; runtime discovery never mutates it. */
export interface SagManagerStore {
  load(): Promise<SagConnectionDescriptor | undefined>
}

/** Injectable connection lifecycle operations. */
export interface SagConnectionManagerDeps {
  readonly store: SagManagerStore
  readonly discover: (signal: AbortSignal) => Promise<DiscoveryResult>
  readonly inspect?: (descriptor: SagConnectionDescriptor, signal: AbortSignal) => Promise<ConnectionInspection>
  readonly gateway?: (descriptor: SagConnectionDescriptor) => SagGateway
  /** Hard deadline for one complete discovery and inspection flight. */
  readonly requestTimeoutMs?: number
  /** Maximum reuse window for a matching validated descriptor and gateway. */
  readonly readyCacheTtlMs?: number
}

function errorMessage(error: unknown, descriptor: SagConnectionDescriptor): string {
  const message = error instanceof Error ? error.message : String(error)
  return message.split(descriptor.accessToken).join('<redacted>')
}

/** Run health, readiness, capabilities, source count, and MCP compatibility checks without mutations. */
export async function inspectSagConnection(
  descriptor: SagConnectionDescriptor,
  signal: AbortSignal,
  gateway: SagGateway = createSagGateway(new SagApiClient(descriptor)),
  mcpProbe: (descriptor: SagConnectionDescriptor, signal: AbortSignal, capabilities: readonly string[]) => Promise<McpProbeResult>
    = (candidate, candidateSignal, capabilities) => probeMcp(candidate, candidateSignal, undefined, capabilities),
): Promise<ConnectionInspection> {
  const errors: string[] = []
  let health = false
  try {
    await gateway.health(signal)
    health = true
  } catch (error) {
    errors.push(errorMessage(error, descriptor))
    return { status: 'unreachable', health, ready: false, sourceCount: 0, errors }
  }

  const [readyResult, capabilitiesResult] = await Promise.allSettled([
    gateway.ready(signal),
    gateway.capabilities(signal),
  ])
  if (signal.aborted) throw signal.reason
  const supportsSourceList = capabilitiesResult.status === 'fulfilled'
    && capabilitiesResult.value.capabilities.includes('sources.list')
  const sourcesResult: PromiseSettledResult<readonly unknown[]> = supportsSourceList
    ? await gateway.listSources(signal).then(
      value => ({ status: 'fulfilled', value }),
      reason => ({ status: 'rejected', reason }),
    )
    : { status: 'fulfilled', value: [] }
  if (signal.aborted) throw signal.reason
  const mcpResult: PromiseSettledResult<McpProbeResult> = capabilitiesResult.status === 'fulfilled'
    ? await mcpProbe(descriptor, signal, capabilitiesResult.value.capabilities).then(
      value => ({ status: 'fulfilled', value }),
      reason => ({ status: 'rejected', reason }),
    )
    : { status: 'rejected', reason: capabilitiesResult.reason }
  if (signal.aborted) throw signal.reason
  const ready = readyResult.status === 'fulfilled'
  const capabilities = capabilitiesResult.status === 'fulfilled' ? capabilitiesResult.value : undefined
  const sourceCount = sourcesResult.status === 'fulfilled' ? sourcesResult.value.length : 0
  const mcp = mcpResult.status === 'fulfilled' ? mcpResult.value : undefined
  for (const result of [readyResult, capabilitiesResult, ...(supportsSourceList ? [sourcesResult] : []), ...(capabilitiesResult.status === 'fulfilled' ? [mcpResult] : [])]) {
    if (result.status === 'rejected') errors.push(errorMessage(result.reason, descriptor))
  }
  let status: SagConnectionStatus
  if (!ready || (supportsSourceList && sourcesResult.status === 'rejected')) {
    status = 'unreachable'
  } else if (capabilitiesResult.status === 'rejected') {
    status = capabilitiesResult.reason instanceof SagApiError ? 'unreachable' : 'incompatible'
  } else if (mcpResult.status === 'rejected') {
    status = mcpResult.reason instanceof McpProbeIncompatibleError ? 'incompatible' : 'unreachable'
  } else {
    status = 'ready'
  }
  return {
    status,
    health,
    ready,
    sourceCount,
    ...(capabilities === undefined ? {} : { capabilities }),
    ...(mcp === undefined ? {} : { mcp }),
    ...(errors.length === 0 ? {} : { errors }),
  }
}

function waitForCaller<T>(promise: Promise<T>, signal: AbortSignal): Promise<T> {
  if (signal.aborted) return Promise.reject(signal.reason)
  return new Promise<T>((resolve, reject) => {
    const onAbort = () => reject(signal.reason)
    signal.addEventListener('abort', onAbort, { once: true })
    promise.then(resolve, reject).finally(() => signal.removeEventListener('abort', onAbort)).catch(() => undefined)
  })
}

/** Lazy, single-flight manager for saved and automatically discovered local SAG connections. */
export class SagConnectionManager {
  private flight: { readonly key: string; readonly promise: Promise<SagConnectionReport> } | undefined
  private connected: SagConnectionReport | undefined
  private readonly gateway: (descriptor: SagConnectionDescriptor) => SagGateway
  private readonly requestTimeoutMs: number
  private readonly readyCacheTtlMs: number
  private connectedAt = 0
  private generation = 0
  private requestSequence = 0
  private latestAdmittedRequest = 0

  /** @param deps - saved-connection reader, discovery, and optional testable network seams. */
  constructor(private readonly deps: SagConnectionManagerDeps) {
    this.gateway = deps.gateway ?? (descriptor => createSagGateway(new SagApiClient(descriptor)))
    this.requestTimeoutMs = deps.requestTimeoutMs ?? 30_000
    this.readyCacheTtlMs = deps.readyCacheTtlMs ?? 5_000
  }

  private report(descriptor: SagConnectionDescriptor, gateway: SagGateway, inspection: ConnectionInspection, discovery?: DiscoveryResult): SagConnectionReport {
    return {
      ...inspection,
      descriptor,
      gateway,
      ...(discovery === undefined ? {} : { discovery }),
    }
  }

  private async inspectCandidate(
    descriptor: SagConnectionDescriptor,
    signal: AbortSignal,
    assertOwned: () => void,
  ): Promise<{ inspection: ConnectionInspection; gateway: SagGateway }> {
    const gateway = this.gateway(descriptor)
    try {
      const inspection = this.deps.inspect === undefined
        ? await inspectSagConnection(descriptor, signal, gateway)
        : await this.deps.inspect(descriptor, signal)
      assertOwned()
      return { inspection, gateway }
    } catch (error) {
      this.closeDetached(gateway)
      throw error
    }
  }

  private async connect(saved: SagConnectionDescriptor | undefined, signal: AbortSignal, assertOwned: () => void): Promise<SagConnectionReport> {
    let savedInspection: ConnectionInspection | undefined
    if (saved !== undefined) {
      const candidate = await this.inspectCandidate(saved, signal, assertOwned)
      savedInspection = candidate.inspection
      if (savedInspection.status === 'ready') return this.report(saved, candidate.gateway, savedInspection)
      this.closeDetached(candidate.gateway)
    }

    const discovery = await this.deps.discover(signal)
    assertOwned()
    if (discovery.descriptor === undefined) {
      if (saved !== undefined && savedInspection !== undefined) return { ...savedInspection, descriptor: saved, discovery }
      return { status: 'not-found', health: false, ready: false, sourceCount: 0, discovery }
    }

    const candidate = await this.inspectCandidate(discovery.descriptor, signal, assertOwned)
    const report = this.report(discovery.descriptor, candidate.gateway, candidate.inspection, discovery)
    return report
  }

  private deadline<T>(operation: (signal: AbortSignal) => Promise<T>): Promise<T> {
    const controller = new AbortController()
    let timer: ReturnType<typeof setTimeout> | undefined
    const timeout = new Promise<never>((_resolve, reject) => {
      timer = setTimeout(() => {
        const error = new Error(`dsh-sag: SAG connection inspection timed out after ${this.requestTimeoutMs} ms`)
        controller.abort(error)
        reject(error)
      }, this.requestTimeoutMs)
    })
    const running = operation(controller.signal)
    running.catch(() => undefined)
    return Promise.race([running, timeout]).finally(() => {
      if (timer !== undefined) clearTimeout(timer)
    })
  }

  private static fingerprint(descriptor: SagConnectionDescriptor | undefined): string {
    if (descriptor === undefined) return 'discover'
    return JSON.stringify([
      descriptor.schemaVersion, descriptor.name, descriptor.apiUrl, descriptor.mcpUrl,
      descriptor.accessToken, descriptor.defaultSourceId ?? null,
    ])
  }

  private closeDetached(gateway: SagGateway | undefined): void {
    if (gateway?.close === undefined) return
    try {
      const closing = Promise.resolve(gateway.close())
      const bounded = new Promise<void>(resolve => {
        const timer = setTimeout(resolve, Math.min(this.requestTimeoutMs, 1_000))
        timer.unref?.()
      })
      void Promise.race([closing, bounded]).catch(() => undefined)
    } catch {
      // Obsolete optional gateway cleanup cannot invalidate a new ready connection.
    }
  }

  private replaceConnected(report: SagConnectionReport): void {
    const previous = this.connected
    this.connected = report.status === 'ready' ? report : undefined
    this.connectedAt = report.status === 'ready' ? Date.now() : 0
    if (previous?.gateway !== report.gateway) this.closeDetached(previous?.gateway)
    if (report.status !== 'ready') this.closeDetached(report.gateway)
  }

  private reusableConnected(saved: SagConnectionDescriptor | undefined): SagConnectionReport | undefined {
    if (this.connected === undefined || Date.now() - this.connectedAt >= this.readyCacheTtlMs) return undefined
    const matches = saved === undefined
      ? this.connected.discovery?.descriptor !== undefined
      : SagConnectionManager.fingerprint(this.connected.descriptor) === SagConnectionManager.fingerprint(saved)
    return matches ? this.connected : undefined
  }

  /** Load the caller's latest saved descriptor, then share only a matching connection flight. */
  async ensureConnected(signal: AbortSignal): Promise<SagConnectionReport> {
    if (signal.aborted) return Promise.reject(signal.reason)
    const request = ++this.requestSequence
    const admission = this.deadline(async callerDeadlineSignal => {
      const assertCallerActive = () => {
        if (signal.aborted) throw signal.reason
        if (callerDeadlineSignal.aborted) throw callerDeadlineSignal.reason
      }
      const saved = await this.deps.store.load()
      assertCallerActive()
      const key = SagConnectionManager.fingerprint(saved)

      if (request < this.latestAdmittedRequest) {
        const reusable = this.reusableConnected(saved)
        if (reusable !== undefined) return reusable
        if (this.flight?.key === key) return waitForCaller(this.flight.promise, callerDeadlineSignal)
        throw new Error('dsh-sag: connection request was superseded')
      }
      this.latestAdmittedRequest = request

      if (this.flight !== undefined && this.flight.key !== key) {
        ++this.generation
        this.flight = undefined
      }
      const reusable = this.reusableConnected(saved)
      if (reusable !== undefined) return reusable
      if (this.flight?.key === key) return waitForCaller(this.flight.promise, callerDeadlineSignal)

      const generation = ++this.generation
      const promise = this.deadline(async sharedSignal => {
        const assertOwned = () => {
          if (sharedSignal.aborted) throw sharedSignal.reason
          if (generation !== this.generation) throw new Error('dsh-sag: connection flight was superseded')
        }
        let report: SagConnectionReport | undefined
        try {
          report = await this.connect(saved, sharedSignal, assertOwned)
          assertOwned()
          this.replaceConnected(report)
          return report
        } catch (error) {
          if (report?.gateway !== this.connected?.gateway) this.closeDetached(report?.gateway)
          throw error
        }
      })
      const flight = { key, promise }
      this.flight = flight
      promise.finally(() => {
        if (this.flight === flight) this.flight = undefined
      }).catch(() => undefined)
      return waitForCaller(promise, callerDeadlineSignal)
    })
    return waitForCaller(admission, signal)
  }

  /** Re-run only non-mutating connection checks for setup and diagnostics. */
  async doctor(signal: AbortSignal): Promise<SagConnectionReport> {
    if (signal.aborted) return Promise.reject(signal.reason)
    return waitForCaller(this.deadline(async sharedSignal => {
      const assertActive = () => { if (sharedSignal.aborted) throw sharedSignal.reason }
      const saved = await this.deps.store.load()
      assertActive()
      return this.connect(saved, sharedSignal, assertActive)
    }), signal)
  }
}
