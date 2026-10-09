import {
  decodeLocalEvidenceRef,
  encodeLocalEvidenceRef,
  type CreateSourceRequest,
  type SagApiClient,
  type SagChunk,
  type SagDocument,
  type SagFileUpload,
  type SagReprocessJob,
  type SagSearchRequest,
  type SagSource,
  type SagTextIngest,
} from './api-client.js'
import type { SagCapabilityDescriptor } from '../connection/types.js'

/** One search section with an opaque read reference for the model-facing tool. */
export interface SagEvidence {
  readonly evidenceRef: string
  readonly sourceId: string
  readonly sourceName: string | null
  readonly chunkId: string
  readonly heading: string
  readonly content: string
  readonly score: number
  readonly rank: number
}

/** Stable local search output independent of SAG's wire field names. */
export interface SagGatewaySearchResult {
  readonly query: string
  readonly evidences: readonly SagEvidence[]
  readonly summary: string
  readonly stats: Readonly<Record<string, unknown>>
}

/** Stable operations consumed by local dsh-sag tools and diagnostics. */
export interface SagGateway {
  /** Release optional transport resources owned by a custom gateway. */
  close?(): void | Promise<void>
  health(signal: AbortSignal): Promise<unknown>
  ready(signal: AbortSignal): Promise<unknown>
  capabilities(signal: AbortSignal): Promise<SagCapabilityDescriptor>
  listSources(signal: AbortSignal): Promise<readonly SagSource[]>
  createSource(request: CreateSourceRequest, signal: AbortSignal): Promise<SagSource>
  search(request: SagSearchRequest, signal: AbortSignal): Promise<SagGatewaySearchResult>
  read(request: { readonly evidenceRef: string }, signal: AbortSignal): Promise<SagChunk>
  listDocuments(sourceId: string, signal: AbortSignal): Promise<readonly SagDocument[]>
  getDocument(sourceId: string, documentId: string, signal: AbortSignal): Promise<SagDocument>
  uploadFile(request: SagFileUpload, signal: AbortSignal): Promise<SagDocument>
  ingestText(request: SagTextIngest, signal: AbortSignal): Promise<SagDocument>
  reprocessDocument(sourceId: string, documentId: string, signal: AbortSignal): Promise<SagReprocessJob>
  deleteDocument(sourceId: string, documentId: string, signal: AbortSignal): Promise<unknown>
}

/** Adapt the SAG REST client to stable camel-case domain operations. */
export function createSagGateway(api: SagApiClient): SagGateway {
  return {
    health: signal => api.health(signal),
    ready: signal => api.ready(signal),
    capabilities: signal => api.capabilities(signal),
    listSources: signal => api.listSources(signal),
    createSource: (request, signal) => api.createSource(request, signal),
    async search(request, signal) {
      const result = await api.search(request, signal)
      return {
        query: result.query,
        summary: result.summary,
        stats: result.stats,
        evidences: result.sections.map(section => ({
          ...section,
          evidenceRef: encodeLocalEvidenceRef({ v: 1, sourceId: section.sourceId, chunkId: section.chunkId }),
        })),
      }
    },
    read(request, signal) {
      const ref = decodeLocalEvidenceRef(request.evidenceRef)
      return api.readChunk(ref.sourceId, ref.chunkId, signal)
    },
    listDocuments: (sourceId, signal) => api.listDocuments(sourceId, signal),
    getDocument: (sourceId, documentId, signal) => api.getDocument(sourceId, documentId, signal),
    uploadFile: (request, signal) => api.uploadFile(request, signal),
    ingestText: (request, signal) => api.ingestText(request, signal),
    reprocessDocument: (sourceId, documentId, signal) => api.reprocessDocument(sourceId, documentId, signal),
    deleteDocument: (sourceId, documentId, signal) => api.deleteDocument(sourceId, documentId, signal),
  }
}
