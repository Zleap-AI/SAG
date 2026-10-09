import { Buffer } from 'node:buffer'
import type { SagEvidenceRef, SagNamespaceId } from '../brand.ts'

export const RPC_PROTOCOL_VERSION = '1.0' as const
export const REQUIRED_ENGINE_VERSION = '0.14.0' as const
export const MAX_FRAME_BYTES = 8 * 1024 * 1024

export type RpcId = number
export type SearchMode = 'fast' | 'precise'

export interface InitializeResult {
  readonly protocolVersion: string
  readonly engineVersion: string
  readonly health: 'available' | 'degraded' | 'unavailable'
  readonly evidenceRead: boolean
  readonly namespaces: readonly string[]
}

export interface SearchEvidence {
  readonly evidenceRef: SagEvidenceRef
  readonly namespaceId: SagNamespaceId
  readonly sourceId: string
  readonly title: string
  readonly excerpt: string
  readonly score?: number
}

export interface SearchResult {
  readonly query: string
  readonly evidences: readonly SearchEvidence[]
}

export interface ReadEvent {
  readonly id?: string
  readonly title?: string
  readonly summary?: string
  readonly category?: string
  readonly rank?: number
}

export interface ReadResult {
  readonly title: string
  readonly content: string
  readonly offset: number
  readonly nextOffset?: number
  readonly totalChars: number
  readonly events?: readonly ReadEvent[]
}

export interface RpcErrorData {
  readonly code: string
  readonly operation?: string
  readonly stage?: string
  readonly retryable?: boolean
  readonly provider?: string
  readonly itemId?: string
  readonly message?: string
  readonly details?: Readonly<Record<string, unknown>>
}

export interface RpcError {
  readonly code: number
  readonly message: string
  readonly data?: RpcErrorData
}

export type RpcResult = InitializeResult | SearchResult | ReadResult | Readonly<Record<string, never>>
export type RpcResponse =
  | { readonly jsonrpc: '2.0'; readonly id: RpcId; readonly result: RpcResult }
  | { readonly jsonrpc: '2.0'; readonly id: RpcId; readonly error: RpcError }

export type RpcRequest =
  | { readonly jsonrpc: '2.0'; readonly id: RpcId; readonly method: 'initialize'; readonly params: { readonly protocolVersion: '1.0' } }
  | { readonly jsonrpc: '2.0'; readonly id: RpcId; readonly method: 'search'; readonly params: { readonly query: string; readonly namespaces: readonly string[]; readonly mode: SearchMode; readonly limit: number } }
  | { readonly jsonrpc: '2.0'; readonly id: RpcId; readonly method: 'read'; readonly params: { readonly evidenceRef: string; readonly offset: number; readonly maxChars: number; readonly includeEvents: boolean } }
  | { readonly jsonrpc: '2.0'; readonly id: RpcId; readonly method: 'shutdown'; readonly params: Readonly<Record<string, never>> }
  | { readonly jsonrpc: '2.0'; readonly method: '$/cancelRequest'; readonly params: { readonly id: RpcId } }

/** A safe protocol failure that identifies only the frame correlation point. */
export class SagProtocolError extends Error {
  override readonly name = 'SagProtocolError'
}

function object(value: unknown, label: string): Record<string, unknown> {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) {
    throw new SagProtocolError(`${label} must be an object`)
  }
  return value as Record<string, unknown>
}

function exactKeys(value: Record<string, unknown>, required: readonly string[], optional: readonly string[], label: string): void {
  const allowed = new Set([...required, ...optional])
  if (required.some(key => !(key in value)) || Object.keys(value).some(key => !allowed.has(key))) {
    throw new SagProtocolError(`${label} contains invalid fields`)
  }
}

function validId(value: unknown): value is RpcId {
  return typeof value === 'number' && Number.isSafeInteger(value) && value >= 0
}

function validateResult(value: unknown, id: number): RpcResult {
  const result = object(value, `response id ${id} result`)
  const keys = Object.keys(result)
  if (keys.length === 0) return {}
  if ('protocolVersion' in result) {
    exactKeys(result, ['protocolVersion', 'engineVersion', 'health', 'evidenceRead', 'namespaces'], [], `response id ${id} initialize result`)
    if (typeof result.protocolVersion !== 'string' || typeof result.engineVersion !== 'string'
      || !['available', 'degraded', 'unavailable'].includes(String(result.health))
      || typeof result.evidenceRead !== 'boolean'
      || !Array.isArray(result.namespaces) || !result.namespaces.every(item => typeof item === 'string')) {
      throw new SagProtocolError(`response id ${id} initialize result is invalid`)
    }
    return result as unknown as InitializeResult
  }
  if ('evidences' in result) {
    exactKeys(result, ['query', 'evidences'], [], `response id ${id} search result`)
    if (typeof result.query !== 'string' || !Array.isArray(result.evidences)) {
      throw new SagProtocolError(`response id ${id} search result is invalid`)
    }
    for (const raw of result.evidences) {
      const evidence = object(raw, `response id ${id} evidence`)
      exactKeys(evidence, ['evidenceRef', 'namespaceId', 'sourceId', 'title', 'excerpt'], ['score'], `response id ${id} evidence`)
      if (['evidenceRef', 'namespaceId', 'sourceId', 'title', 'excerpt'].some(key => typeof evidence[key] !== 'string')
        || ('score' in evidence && typeof evidence.score !== 'number')) {
        throw new SagProtocolError(`response id ${id} evidence is invalid`)
      }
    }
    return result as unknown as SearchResult
  }
  if ('content' in result) {
    exactKeys(result, ['title', 'content', 'offset', 'totalChars'], ['nextOffset', 'events'], `response id ${id} read result`)
    if (typeof result.title !== 'string' || typeof result.content !== 'string'
      || !Number.isSafeInteger(result.offset) || (result.offset as number) < 0
      || !Number.isSafeInteger(result.totalChars) || (result.totalChars as number) < 0
      || ('nextOffset' in result && (!Number.isSafeInteger(result.nextOffset) || (result.nextOffset as number) < 0))
      || ('events' in result && !Array.isArray(result.events))) {
      throw new SagProtocolError(`response id ${id} read result is invalid`)
    }
    return result as unknown as ReadResult
  }
  throw new SagProtocolError(`response id ${id} result contains invalid fields`)
}

/** Decode and validate one JSON-RPC response frame from the Python process. */
export function decodeResponse(line: string): RpcResponse {
  if (Buffer.byteLength(line, 'utf8') > MAX_FRAME_BYTES) {
    throw new SagProtocolError('response frame exceeds 8 MiB')
  }
  let raw: unknown
  try {
    raw = JSON.parse(line)
  } catch {
    throw new SagProtocolError('response frame is not valid JSON')
  }
  const response = object(raw, 'response')
  const idLabel = validId(response.id) ? ` id ${response.id}` : ''
  exactKeys(response, ['jsonrpc', 'id'], ['result', 'error'], `response${idLabel}`)
  if (response.jsonrpc !== '2.0' || !validId(response.id)) {
    throw new SagProtocolError(`response${idLabel} has invalid JSON-RPC metadata`)
  }
  if (('result' in response) === ('error' in response)) {
    throw new SagProtocolError(`response id ${response.id} must contain exactly one of result or error`)
  }
  if ('result' in response) {
    return { jsonrpc: '2.0', id: response.id, result: validateResult(response.result, response.id) }
  }
  const error = object(response.error, `response id ${response.id} error`)
  exactKeys(error, ['code', 'message'], ['data'], `response id ${response.id} error`)
  if (!Number.isInteger(error.code) || typeof error.message !== 'string') {
    throw new SagProtocolError(`response id ${response.id} error is invalid`)
  }
  let data: RpcErrorData | undefined
  if ('data' in error) {
    const rawData = object(error.data, `response id ${response.id} error data`)
    exactKeys(rawData, ['code'], ['operation', 'stage', 'retryable', 'provider', 'itemId', 'message', 'details'], `response id ${response.id} error data`)
    if (typeof rawData.code !== 'string') throw new SagProtocolError(`response id ${response.id} error data is invalid`)
    data = rawData as unknown as RpcErrorData
  }
  return {
    jsonrpc: '2.0',
    id: response.id,
    error: { code: error.code as number, message: error.message, ...(data ? { data } : {}) },
  }
}

/** Encode one validated request as a single NDJSON frame. */
export function encodeRequest(request: RpcRequest): string {
  const line = `${JSON.stringify(request)}\n`
  if (Buffer.byteLength(line, 'utf8') > MAX_FRAME_BYTES) {
    throw new SagProtocolError('request frame exceeds 8 MiB')
  }
  return line
}
