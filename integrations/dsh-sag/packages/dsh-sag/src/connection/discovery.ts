import { posix, win32 } from 'node:path'
import type { FileSystem } from '@deepseek-ai/dsh-fs'
import { parseConnectionDescriptor } from './descriptor.js'
import { SAG_SETUP_COMMAND } from './guidance.js'
import type { SagConnectionDescriptor } from './types.js'

const LOOPBACK_CONNECTION_PATH = '/api/v1/system/dsh-connection'
const LOOPBACK_TIMEOUT_MS = 1_500

/** An HTTP response sufficient to parse a SAG connection descriptor. */
export interface DiscoveryHttpResponse {
  readonly ok: boolean
  readonly status: number
  json(): Promise<unknown>
}

/** The injected HTTP boundary used for loopback discovery. */
export type DiscoveryFetch = (url: string, init: { readonly signal: AbortSignal }) => Promise<DiscoveryHttpResponse>

/** One failed discovery candidate with a remediation a caller can present. */
export interface DiscoveryDiagnostic {
  readonly source: 'file' | 'loopback'
  readonly candidate: string
  readonly message: string
  readonly action: string
}

/** A connection found without changing the saved dsh-sag settings. */
export interface DiscoveryResult {
  readonly source: 'file' | 'loopback' | undefined
  readonly descriptor: SagConnectionDescriptor | undefined
  readonly diagnostics: readonly DiscoveryDiagnostic[]
}

/** The filesystem and network candidates to inspect in their supplied order. */
export interface DiscoverConnectionOptions {
  readonly fs: Pick<FileSystem, 'resolve' | 'readText'>
  readonly paths: readonly string[]
  readonly urls: readonly string[]
  readonly fetch: DiscoveryFetch
}

function joinForPlatform(platform: NodeJS.Platform, ...parts: string[]): string {
  return (platform === 'win32' ? win32 : posix).join(...parts)
}

/** Return the one explicit export path or the platform-standard SAG connection path. */
export function platformConnectionPaths(
  env: Readonly<NodeJS.ProcessEnv>,
  platform: NodeJS.Platform,
  home: string,
): readonly string[] {
  const explicit = env.SAG_DSH_CONNECTION_FILE
  if (explicit) return [explicit]
  if (platform === 'darwin') {
    return [joinForPlatform(platform, home, 'Library', 'Application Support', 'SAG', 'dsh-connection.json')]
  }
  if (platform === 'win32') {
    return [joinForPlatform(platform, env.APPDATA || joinForPlatform(platform, home, 'AppData', 'Roaming'), 'SAG', 'dsh-connection.json')]
  }
  return [joinForPlatform(platform, env.XDG_CONFIG_HOME || joinForPlatform(platform, home, '.config'), 'sag', 'dsh-connection.json')]
}

function message(error: unknown): string {
  return error instanceof Error && error.message ? error.message : String(error)
}

function abortIfNeeded(signal: AbortSignal): void {
  if (signal.aborted) throw signal.reason
}

function fileDiagnostic(candidate: string, error: unknown): DiscoveryDiagnostic {
  return {
    source: 'file',
    candidate,
    message: message(error),
    action: `Check the SAG connection export at ${candidate} or run ${SAG_SETUP_COMMAND}.`,
  }
}

function loopbackDiagnostic(candidate: string, error: unknown): DiscoveryDiagnostic {
  return {
    source: 'loopback',
    candidate,
    message: message(error),
    action: `Start SAG for ${candidate} or run ${SAG_SETUP_COMMAND} --url <SAG loopback URL>.`,
  }
}

function safeLoopbackCandidate(url: string): string {
  try {
    const parsed = new URL(url)
    parsed.username = ''
    parsed.password = ''
    return parsed.toString()
  } catch {
    return url.replace(/(\/\/)[^/?#]*@/, '$1<redacted>@')
  }
}

function connectionEndpoint(url: string): string {
  const parsed = new URL(url)
  if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') {
    throw new Error('discovery URL must be an http or https URL')
  }
  if (parsed.username || parsed.password) {
    throw new Error('discovery URL must not include a username or password')
  }
  return new URL(LOOPBACK_CONNECTION_PATH, parsed).toString()
}

async function fetchWithTimeout(fetch: DiscoveryFetch, url: string, signal: AbortSignal): Promise<DiscoveryHttpResponse> {
  abortIfNeeded(signal)
  const controller = new AbortController()
  let rejectCallerAbort!: (reason?: unknown) => void
  const callerAbort = new Promise<never>((_resolve, reject) => { rejectCallerAbort = reject })
  const abortFromCaller = () => {
    controller.abort(signal.reason)
    rejectCallerAbort(signal.reason)
  }
  signal.addEventListener('abort', abortFromCaller, { once: true })
  let timer: ReturnType<typeof setTimeout> | undefined
  const timeout = new Promise<never>((_resolve, reject) => {
    timer = setTimeout(() => {
      const error = new Error(`timed out after ${LOOPBACK_TIMEOUT_MS}ms`)
      controller.abort(error)
      reject(error)
    }, LOOPBACK_TIMEOUT_MS)
  })
  try {
    const response = await Promise.race([fetch(url, { signal: controller.signal }), timeout, callerAbort])
    abortIfNeeded(signal)
    return response
  } finally {
    if (timer !== undefined) clearTimeout(timer)
    signal.removeEventListener('abort', abortFromCaller)
  }
}

/**
 * Inspect connection files before configured loopback URLs, returning the first strict v1 descriptor.
 * Discovery never saves a descriptor; its caller decides whether a valid result should be persisted.
 */
export async function discoverConnection(options: DiscoverConnectionOptions, signal: AbortSignal): Promise<DiscoveryResult> {
  const diagnostics: DiscoveryDiagnostic[] = []
  for (const candidate of options.paths) {
    abortIfNeeded(signal)
    try {
      const target = await options.fs.resolve(candidate, { signal })
      const text = await options.fs.readText(target, signal)
      let value: unknown
      try {
        value = JSON.parse(text)
      } catch (error) {
        throw new Error(`invalid JSON: ${message(error)}`)
      }
      return { source: 'file', descriptor: parseConnectionDescriptor(value), diagnostics }
    } catch (error) {
      if (signal.aborted) throw error
      diagnostics.push(fileDiagnostic(candidate, error))
    }
  }
  for (const url of options.urls) {
    abortIfNeeded(signal)
    let candidate = safeLoopbackCandidate(url)
    try {
      candidate = connectionEndpoint(url)
      const response = await fetchWithTimeout(options.fetch, candidate, signal)
      if (!response.ok) throw new Error(`HTTP ${response.status}`)
      return { source: 'loopback', descriptor: parseConnectionDescriptor(await response.json()), diagnostics }
    } catch (error) {
      if (signal.aborted) throw error
      diagnostics.push(loopbackDiagnostic(candidate, error))
    }
  }
  return { source: undefined, descriptor: undefined, diagnostics }
}
