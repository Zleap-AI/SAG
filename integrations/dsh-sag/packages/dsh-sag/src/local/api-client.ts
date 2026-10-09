import type { SagCapabilityDescriptor, SagConnectionDescriptor } from '../connection/types.js'

/** One SAG source projected through its public REST fields. */
export interface SagSource {
  readonly id: string
  readonly name: string
  readonly description?: string
  readonly status?: string
  readonly documentCount?: number
  readonly chunkCount?: number
  readonly [field: string]: unknown
}

/** Parameters accepted by SAG's source creation endpoint. */
export interface CreateSourceRequest {
  readonly name: string
  readonly description?: string
  readonly connectorKind?: string
  readonly config?: Readonly<Record<string, unknown>>
}

/** Parameters for structured workspace search. */
export interface SagSearchRequest {
  readonly query: string
  readonly sourceIds?: readonly string[]
  readonly topK?: number
  readonly strategy?: string
}

/** One structured evidence section returned by SAG. */
export interface SagSearchSection {
  readonly sourceId: string
  readonly sourceName: string | null
  readonly chunkId: string
  readonly heading: string
  readonly content: string
  readonly score: number
  readonly rank: number
}

/** Structured search response used by the local gateway. */
export interface SagSearchResult {
  readonly query: string
  readonly sections: readonly SagSearchSection[]
  readonly summary: string
  readonly stats: Readonly<Record<string, unknown>>
}

/** Bytes already admitted by dsh FS for one multipart upload. */
export interface SagFileUpload {
  readonly sourceId: string
  readonly filename: string
  readonly contentType: string
  readonly bytes: Uint8Array
}

/** Text content to ingest into one source. */
export interface SagTextIngest {
  readonly sourceId: string
  readonly text: string
  readonly title?: string
  readonly messages?: readonly Readonly<Record<string, unknown>>[]
}

/** A source-bound SAG document or processing job. */
export interface SagDocument {
  readonly id: string
  readonly sourceId?: string
  readonly filename?: string
  readonly status?: 'pending' | 'loading' | 'extracting' | 'pausing' | 'paused' | 'deleting' | 'delete_failed' | 'ready' | 'failed'
  readonly progress?: number
  readonly chunkCount?: number
  readonly error?: string | null
  readonly errorLayer?: string | null
  readonly errorStage?: string | null
  readonly [field: string]: unknown
}

/** One asynchronous SAG processing job returned by document mutations. */
export interface SagJob {
  readonly id: string
  readonly status: 'queued' | 'running' | 'paused' | 'succeeded' | 'failed'
  readonly sourceId: string | null
  readonly documentId: string | null
  readonly type: 'process_document' | 'reprocess_document' | 'delete_document' | 'sync_source' | 'index_universe' | 'octx_preflight' | 'octx_import' | 'octx_export' | 'octx_gc_installation' | 'octx_gc_transfer'
  readonly progress?: number
  readonly attempts?: number
  readonly error?: string | null
}

/** Reprocess endpoint job with its operation-specific type. */
export interface SagReprocessJob extends SagJob {
  readonly type: 'reprocess_document'
}

/** Full content for one source chunk. */
export interface SagChunk {
  readonly sourceId: string
  readonly chunkId: string
  readonly content: string
  readonly [field: string]: unknown
}

/** Versioned local reference emitted by search and accepted by read. */
export interface LocalEvidenceRef {
  readonly v: 1
  readonly sourceId: string
  readonly chunkId: string
}

interface SagErrorPayload {
  readonly code: string
  readonly message: string
  readonly retryable?: boolean
  readonly request_id?: string
  readonly layer?: string
  readonly stage?: string
}

/** A non-success SAG response with its stable public error fields. */
export class SagApiError extends Error {
  /**
   * @param status - HTTP status returned by SAG.
   * @param code - stable SAG error code.
   * @param message - redacted public error message.
   * @param retryable - whether SAG considers a retry safe.
   * @param requestId - optional SAG request identifier.
   * @param layer - public SAG responsibility category.
   * @param stage - public SAG processing stage.
   */
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
    readonly retryable: boolean | undefined,
    readonly requestId: string | undefined,
    readonly layer: string | undefined,
    readonly stage: string | undefined,
  ) {
    super(`SAG API ${status} ${code}: ${message}`)
    this.name = 'SagApiError'
  }
}

function object(value: unknown, label: string): Record<string, unknown> {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) throw new Error(`dsh-sag: ${label} must be an object`)
  return value as Record<string, unknown>
}

function string(value: unknown, label: string): string {
  if (typeof value !== 'string' || !value) throw new Error(`dsh-sag: ${label} must be a non-empty string`)
  return value
}

function number(value: unknown, label: string): number {
  if (typeof value !== 'number' || !Number.isFinite(value)) throw new Error(`dsh-sag: ${label} must be a finite number`)
  return value
}

function recordWithCamelIds(value: unknown, label: string): Record<string, unknown> {
  const source = object(value, label)
  const result = { ...source }
  if (typeof source.source_id === 'string') result.sourceId = source.source_id
  if (typeof source.chunk_id === 'string') result.chunkId = source.chunk_id
  if (typeof source.document_count === 'number') result.documentCount = source.document_count
  if (typeof source.chunk_count === 'number') result.chunkCount = source.chunk_count
  if (source.error === null || typeof source.error === 'string') result.error = source.error
  if (source.error_layer === null || typeof source.error_layer === 'string') result.errorLayer = source.error_layer
  if (source.error_stage === null || typeof source.error_stage === 'string') result.errorStage = source.error_stage
  return result
}

function parseSource(value: unknown): SagSource {
  const result = recordWithCamelIds(value, 'source')
  return { ...result, id: string(result.id, 'source.id'), name: string(result.name, 'source.name') } as SagSource
}

function parseDocument(value: unknown): SagDocument {
  const result = recordWithCamelIds(value, 'document')
  if (result.progress !== undefined && !Number.isInteger(result.progress)) throw new Error('dsh-sag: document.progress must be an integer percentage')
  return { ...result, id: string(result.id, 'document.id') } as SagDocument
}

function nullableString(value: unknown, label: string): string | null {
  if (value === null) return null
  return string(value, label)
}

function parseJob(value: unknown): SagJob {
  const source = object(value, 'job')
  const jobTypes = ['process_document', 'reprocess_document', 'delete_document', 'sync_source', 'index_universe', 'octx_preflight', 'octx_import', 'octx_export', 'octx_gc_installation', 'octx_gc_transfer'] as const
  const jobStatuses = ['queued', 'running', 'paused', 'succeeded', 'failed'] as const
  const type = string(source.type, 'job.type')
  const status = string(source.status, 'job.status')
  if (!(jobTypes as readonly string[]).includes(type)) throw new Error('dsh-sag: job.type is unsupported')
  if (!(jobStatuses as readonly string[]).includes(status)) throw new Error('dsh-sag: job.status is unsupported')
  const optionalProgress = typeof source.progress === 'number' ? { progress: source.progress } : {}
  const optionalAttempts = typeof source.attempts === 'number' ? { attempts: source.attempts } : {}
  const optionalError = source.error === null || typeof source.error === 'string' ? { error: source.error } : {}
  return {
    id: string(source.id, 'job.id'),
    type: type as SagJob['type'],
    status: status as SagJob['status'],
    sourceId: nullableString(source.source_id, 'job.source_id'),
    documentId: nullableString(source.document_id, 'job.document_id'),
    ...optionalProgress,
    ...optionalAttempts,
    ...optionalError,
  }
}

function parseCapability(value: unknown): SagCapabilityDescriptor {
  const root = object(value, 'capability response')
  if (root.schemaVersion !== 1) throw new Error('dsh-sag: capability response requires schemaVersion 1')
  if (!Array.isArray(root.capabilities) || !root.capabilities.every(item => typeof item === 'string')) {
    throw new Error('dsh-sag: capability response capabilities must be strings')
  }
  let upload: SagCapabilityDescriptor['upload']
  if (root.upload !== undefined) {
    const candidate = object(root.upload, 'capability response upload')
    if (!Number.isInteger(candidate.maxMb) || (candidate.maxMb as number) <= 0) {
      throw new Error('dsh-sag: capability response upload.maxMb must be a positive integer')
    }
    if (!Array.isArray(candidate.extensions) || !candidate.extensions.every(item => typeof item === 'string' && item.length > 0 && !item.startsWith('.'))) {
      throw new Error('dsh-sag: capability response upload.extensions must omit leading dots')
    }
    upload = { maxMb: candidate.maxMb as number, extensions: candidate.extensions as string[] }
  }
  const defaultSourceId = root.defaultSourceId
  if (defaultSourceId !== undefined && defaultSourceId !== null && (typeof defaultSourceId !== 'string' || !defaultSourceId)) {
    throw new Error('dsh-sag: capability response defaultSourceId must be a non-empty string or null')
  }
  return {
    schemaVersion: 1,
    capabilities: root.capabilities as string[],
    ...(upload === undefined ? {} : { upload }),
    ...(defaultSourceId === undefined ? {} : { defaultSourceId }),
  }
}

function encodePath(value: string): string {
  return encodeURIComponent(value)
}

/** Encode a canonical local SAG evidence reference. */
export function encodeLocalEvidenceRef(ref: LocalEvidenceRef): string {
  return Buffer.from(JSON.stringify({ v: 1, sourceId: ref.sourceId, chunkId: ref.chunkId }), 'utf8').toString('base64url')
}

/** Decode and strictly validate a canonical local SAG evidence reference. */
export function decodeLocalEvidenceRef(encoded: string): LocalEvidenceRef {
  try {
    if (encoded.length > 4096) throw new Error('evidence reference is too long')
    if (!encoded || !/^[A-Za-z0-9_-]+$/.test(encoded)) throw new Error('invalid alphabet')
    const decoded = Buffer.from(encoded, 'base64url').toString('utf8')
    const value = object(JSON.parse(decoded), 'evidence reference')
    if (Object.keys(value).sort().join(',') !== 'chunkId,sourceId,v') throw new Error('unexpected fields')
    if (value.v !== 1) throw new Error('unsupported version')
    const ref = { v: 1 as const, sourceId: string(value.sourceId, 'evidence reference sourceId'), chunkId: string(value.chunkId, 'evidence reference chunkId') }
    if (encodeLocalEvidenceRef(ref) !== encoded) throw new Error('non-canonical encoding')
    return ref
  } catch (error) {
    throw new Error(`dsh-sag: invalid local evidence reference: ${error instanceof Error ? error.message : String(error)}`)
  }
}

function requestSignal(init: RequestInit, signal: AbortSignal): AbortSignal {
  if (init.signal === undefined || init.signal === null || init.signal === signal) return signal
  return AbortSignal.any([init.signal, signal])
}

/** REST client for the public SAG dsh, source, search, chunk, and document endpoints. */
export class SagApiClient {
  /** @param descriptor - resolved local SAG connection. @param fetch - injectable HTTP implementation. */
  constructor(
    private readonly descriptor: SagConnectionDescriptor,
    private readonly fetch: typeof globalThis.fetch = globalThis.fetch,
  ) {}

  private async requestJson(path: string, init: RequestInit, signal: AbortSignal): Promise<unknown> {
    const headers: Record<string, string> = {}
    if (init.headers instanceof Headers) {
      for (const [name, value] of init.headers.entries()) headers[name] = value
    } else if (Array.isArray(init.headers)) {
      for (const [name, value] of init.headers) headers[name] = value
    } else if (init.headers !== undefined) {
      Object.assign(headers, init.headers)
    }
    headers.Authorization = `Bearer ${this.descriptor.accessToken}`
    let response: Response
    try {
      response = await this.fetch(`${this.descriptor.apiUrl}${path}`, {
        ...init,
        headers,
        signal: requestSignal(init, signal),
      })
    } catch (error) {
      if (signal.aborted) throw signal.reason
      const message = error instanceof Error ? error.message : String(error)
      throw new Error(`dsh-sag: SAG API request failed: ${this.redact(message)}`)
    }
    let payload: unknown
    try {
      payload = await response.json()
    } catch {
      if (!response.ok) throw new SagApiError(response.status, 'http_error', this.redact(response.statusText || 'request failed'), undefined, undefined, undefined, undefined)
      throw new Error(`dsh-sag: SAG API ${response.status} returned invalid JSON`)
    }
    if (!response.ok) {
      const root = typeof payload === 'object' && payload !== null ? payload as Record<string, unknown> : {}
      const candidate = typeof root.error === 'object' && root.error !== null ? root.error as Record<string, unknown> : {}
      const parsed: SagErrorPayload = {
        code: typeof candidate.code === 'string' ? candidate.code : 'http_error',
        message: typeof candidate.message === 'string' ? candidate.message : response.statusText || 'request failed',
        ...(typeof candidate.retryable === 'boolean' ? { retryable: candidate.retryable } : {}),
        ...(typeof candidate.request_id === 'string' ? { request_id: candidate.request_id } : {}),
        ...(typeof candidate.layer === 'string' ? { layer: candidate.layer } : {}),
        ...(typeof candidate.stage === 'string' ? { stage: candidate.stage } : {}),
      }
      throw new SagApiError(
        response.status,
        this.redact(parsed.code),
        this.redact(parsed.message),
        parsed.retryable,
        parsed.request_id === undefined ? undefined : this.redact(parsed.request_id),
        parsed.layer === undefined ? undefined : this.redact(parsed.layer),
        parsed.stage === undefined ? undefined : this.redact(parsed.stage),
      )
    }
    return payload
  }

  private redact(message: string): string {
    return message.split(this.descriptor.accessToken).join('<redacted>')
  }

  private json(method: string, body: unknown): RequestInit {
    return { method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }
  }

  /** Check whether the SAG API process is alive. */
  health(signal: AbortSignal): Promise<unknown> { return this.requestJson('/system/health', {}, signal) }

  /** Check whether SAG storage and knowledge runtime are ready. */
  ready(signal: AbortSignal): Promise<unknown> { return this.requestJson('/system/ready', {}, signal) }

  /** Read the versioned dsh integration capabilities. */
  async capabilities(signal: AbortSignal): Promise<SagCapabilityDescriptor> {
    return parseCapability(await this.requestJson('/system/dsh', {}, signal))
  }

  /** List all local knowledge sources. */
  async listSources(signal: AbortSignal): Promise<readonly SagSource[]> {
    const value = await this.requestJson('/sources', {}, signal)
    if (!Array.isArray(value)) throw new Error('dsh-sag: source list must be an array')
    return value.map(parseSource)
  }

  /** Create one file-upload or configured SAG source. */
  async createSource(request: CreateSourceRequest, signal: AbortSignal): Promise<SagSource> {
    return parseSource(await this.requestJson('/sources', this.json('POST', {
      name: request.name,
      description: request.description ?? '',
      connector_kind: request.connectorKind ?? 'file_upload',
      config: request.config ?? {},
    }), signal))
  }

  /** Search one or more sources using SAG's structured workspace response. */
  async search(request: SagSearchRequest, signal: AbortSignal): Promise<SagSearchResult> {
    const payload = await this.requestJson('/search', this.json('POST', {
      query: request.query,
      ...(request.sourceIds === undefined ? {} : { source_ids: request.sourceIds }),
      ...(request.topK === undefined ? {} : { top_k: request.topK }),
      ...(request.strategy === undefined ? {} : { strategy: request.strategy }),
      save_exploration: false,
    }), signal)
    const root = object(payload, 'search response')
    if (!Array.isArray(root.sections)) throw new Error('dsh-sag: search response sections must be an array')
    const sections = root.sections.flatMap((item, index): readonly SagSearchSection[] => {
      const section = object(item, `search response sections[${index}]`)
      if (section.source_id === null || section.chunk_id === null) return []
      return [{
        sourceId: string(section.source_id, `search section ${index} source_id`),
        sourceName: section.source_name === null || section.source_name === undefined ? null : string(section.source_name, `search section ${index} source_name`),
        chunkId: string(section.chunk_id, `search section ${index} chunk_id`),
        heading: typeof section.heading === 'string' ? section.heading : '',
        content: string(section.content, `search section ${index} content`),
        score: number(section.score, `search section ${index} score`),
        rank: number(section.rank, `search section ${index} rank`),
      }]
    })
    return {
      query: string(root.query, 'search response query'),
      sections,
      summary: typeof root.summary === 'string' ? root.summary : '',
      stats: object(root.stats, 'search response stats'),
    }
  }

  /** Read one complete chunk by its source-local identifiers. */
  async readChunk(sourceId: string, chunkId: string, signal: AbortSignal): Promise<SagChunk> {
    const value = recordWithCamelIds(await this.requestJson(`/sources/${encodePath(sourceId)}/chunks/${encodePath(chunkId)}`, {}, signal), 'chunk')
    const content = typeof value.content === 'string' ? value.content : typeof value.text === 'string' ? value.text : undefined
    return {
      ...value,
      sourceId: typeof value.sourceId === 'string' ? value.sourceId : sourceId,
      chunkId: string(value.chunkId, 'chunk.chunk_id'),
      content: string(content, 'chunk content'),
    } as SagChunk
  }

  /** List documents belonging to one source. */
  async listDocuments(sourceId: string, signal: AbortSignal): Promise<readonly SagDocument[]> {
    const value = await this.requestJson(`/sources/${encodePath(sourceId)}/documents`, {}, signal)
    if (!Array.isArray(value)) throw new Error('dsh-sag: document list must be an array')
    return value.map(parseDocument)
  }

  /** Get one document and its current processing status. */
  async getDocument(sourceId: string, documentId: string, signal: AbortSignal): Promise<SagDocument> {
    return parseDocument(await this.requestJson(`/sources/${encodePath(sourceId)}/documents/${encodePath(documentId)}`, {}, signal))
  }

  /** Upload bytes already read through dsh FS as one multipart document. */
  async uploadFile(upload: SagFileUpload, signal: AbortSignal): Promise<SagDocument> {
    const form = new FormData()
    const bytes = Uint8Array.from(upload.bytes)
    form.set('file', new Blob([bytes.buffer], { type: upload.contentType }), upload.filename)
    return parseDocument(await this.requestJson(`/sources/${encodePath(upload.sourceId)}/documents`, { method: 'POST', body: form }, signal))
  }

  /** Ingest text or messages without waiting for background parsing. */
  async ingestText(ingest: SagTextIngest, signal: AbortSignal): Promise<SagDocument> {
    return parseDocument(await this.requestJson(`/sources/${encodePath(ingest.sourceId)}/documents/ingest`, this.json('POST', {
      text: ingest.text,
      ...(ingest.title === undefined ? {} : { title: ingest.title }),
      ...(ingest.messages === undefined ? {} : { messages: ingest.messages }),
    }), signal))
  }

  /** Queue reprocessing for one document. */
  async reprocessDocument(sourceId: string, documentId: string, signal: AbortSignal): Promise<SagReprocessJob> {
    const job = parseJob(await this.requestJson(`/sources/${encodePath(sourceId)}/documents/${encodePath(documentId)}/reprocess`, { method: 'POST' }, signal))
    if (job.type !== 'reprocess_document') throw new Error('dsh-sag: reprocess job.type must be reprocess_document')
    return job as SagReprocessJob
  }

  /** Delete one document after the caller has obtained dsh approval. */
  async deleteDocument(sourceId: string, documentId: string, signal: AbortSignal): Promise<unknown> {
    return this.requestJson(`/sources/${encodePath(sourceId)}/documents/${encodePath(documentId)}`, { method: 'DELETE' }, signal)
  }
}
