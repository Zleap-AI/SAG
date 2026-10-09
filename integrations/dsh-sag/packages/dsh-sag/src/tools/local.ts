import { FsError } from '@deepseek-ai/dsh-fs'
import type { SagConnectionManager, SagConnectionReport } from '../connection/manager.js'
import { SAG_DOCTOR_COMMAND, SAG_SETUP_COMMAND } from '../connection/guidance.js'
import type { SagConnectionDescriptor, SagCapabilityDescriptor } from '../connection/types.js'
import { SagApiError } from '../local/api-client.js'
import type { SagGateway } from '../local/gateway.js'

export const SAG_NOT_FOUND_MESSAGE = `没有发现正在运行的 SAG。请先启动 SAG 后重试；也可运行 ${SAG_SETUP_COMMAND} 重新发现。`

export interface LocalConnection {
  readonly descriptor: SagConnectionDescriptor
  readonly capabilities: SagCapabilityDescriptor
  readonly gateway: SagGateway
  readonly sourceCount: number
}

export type ConnectionManager = Pick<SagConnectionManager, 'ensureConnected'>

/** Current SAG v1 capability required by each endpoint-backed local Tool. */
export const SAG_TOOL_CAPABILITY = {
  sag_list_sources: 'sources.list',
  sag_create_source: 'sources.create',
  sag_search: 'knowledge.search',
  sag_read: 'knowledge.read',
  sag_list_documents: 'documents.list',
  sag_get_document: 'documents.get',
  sag_upload_file: 'documents.upload',
  sag_ingest_text: 'documents.ingest',
  sag_reprocess_document: 'documents.reprocess',
  sag_delete_document: 'documents.delete',
} as const

export type CapabilityGatedToolName = keyof typeof SAG_TOOL_CAPABILITY

class LocalToolInputError extends Error {}

/** Raise a model-actionable input/configuration failure from inside an operation. */
export function localInputError(message: string): never {
  throw new LocalToolInputError(message)
}

function connectionError(report: SagConnectionReport): Error {
  switch (report.status) {
    case 'not-found':
      return new Error(SAG_NOT_FOUND_MESSAGE)
    case 'unreachable':
      return new Error(`SAG 已发现但当前无法连接。请确认 SAG 正在运行，然后重试或运行 ${SAG_DOCTOR_COMMAND}。`)
    case 'incompatible':
      return new Error(`当前 SAG 版本与 dsh-sag 不兼容。请升级 SAG，然后运行 ${SAG_DOCTOR_COMMAND}。`)
    case 'ready':
      return new Error(`SAG 连接缺少必要信息。请运行 ${SAG_SETUP_COMMAND} 重新配置。`)
  }
}

/** Resolve one ready local connection without exposing credentials or transport details. */
export async function connectLocal(manager: ConnectionManager, signal: AbortSignal): Promise<LocalConnection> {
  let report: SagConnectionReport
  try {
    report = await manager.ensureConnected(signal)
  } catch {
    if (signal.aborted) throw signal.reason
    throw new Error(`无法检查 SAG 连接。请确认 SAG 正在运行，然后重试或运行 ${SAG_DOCTOR_COMMAND}。`)
  }
  if (report.status !== 'ready' || report.descriptor === undefined || report.gateway === undefined || report.capabilities === undefined) {
    throw connectionError(report)
  }
  return { descriptor: report.descriptor, capabilities: report.capabilities, gateway: report.gateway, sourceCount: report.sourceCount }
}

/** Stop before transport when the connected SAG does not advertise an operation. */
export function requireToolCapability(connection: LocalConnection, toolName: CapabilityGatedToolName): void {
  const capability = SAG_TOOL_CAPABILITY[toolName]
  if (!connection.capabilities.capabilities.includes(capability)) {
    localInputError(`This SAG version does not provide ${capability}. Upgrade SAG or use sag_status to inspect available capabilities.`)
  }
}

function redacted(value: string | undefined, token: string): string | undefined {
  return value?.split(token).join('<redacted>')
}

function publicApiError(error: SagApiError, token: string): SagApiError {
  const prefix = `SAG API ${error.status} ${error.code}: `
  const detail = error.message.startsWith(prefix) ? error.message.slice(prefix.length) : error.message
  return new SagApiError(
    error.status,
    redacted(error.code, token)!,
    redacted(detail, token)!,
    error.retryable,
    redacted(error.requestId, token),
    redacted(error.layer, token),
    redacted(error.stage, token),
  )
}

/** Execute one complete local tool operation behind a credential-safe failure boundary. */
export async function localOperation<T>(
  manager: ConnectionManager,
  signal: AbortSignal,
  operation: (connection: LocalConnection) => Promise<T>,
): Promise<T> {
  const connection = await connectLocal(manager, signal)
  try {
    return await operation(connection)
  } catch (error) {
    if (signal.aborted) throw signal.reason
    if (error instanceof LocalToolInputError || error instanceof FsError) throw error
    if (error instanceof SagApiError) throw publicApiError(error, connection.descriptor.accessToken)
    throw new Error(`SAG 连接已中断或返回了不兼容的数据。请确认 SAG 正在运行，然后重试或运行 ${SAG_DOCTOR_COMMAND}。`)
  }
}

/** Choose a source deterministically or return model-actionable candidates. */
export async function selectSource(
  connection: LocalConnection,
  explicitSourceId: string | undefined,
  signal: AbortSignal,
): Promise<string> {
  const explicit = explicitSourceId?.trim()
  if (explicit) return explicit
  const configured = connection.capabilities.defaultSourceId?.trim()
  if (configured) return configured
  if (connection.capabilities.defaultSourceId === undefined) {
    const descriptorDefault = connection.descriptor.defaultSourceId?.trim()
    if (descriptorDefault) return descriptorDefault
  }
  if (!connection.capabilities.capabilities.includes('sources.list')) {
    localInputError('当前 SAG 未提供 sources.list，且没有默认知识库。请为此操作明确指定 source_id。')
  }
  const sources = await connection.gateway.listSources(signal)
  if (sources.length === 1) return sources[0]!.id
  if (sources.length === 0) {
    localInputError('SAG 中还没有知识库。请先用 sag_create_source 创建一个知识库。')
  }
  const choices = sources.map(source => `${source.id} (${source.name})`).join('、')
  localInputError(`发现多个知识库且未设置默认项，请指定知识库 source_id。可选知识库：${choices}`)
}

export function jsonRender(value: unknown) {
  return [{ type: 'text' as const, text: JSON.stringify(value, null, 2) }]
}
