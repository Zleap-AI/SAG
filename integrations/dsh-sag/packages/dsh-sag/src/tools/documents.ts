import type { ToolExecution, PreToolDecision } from '@deepseek-ai/dsh-tools'
import { defineTool } from '@deepseek-ai/dsh-tools'
import type { ResolvedLocalConfig } from '../config.js'
import type { SagDocument } from '../local/api-client.js'
import { jsonRender, localOperation, requireToolCapability, selectSource, type ConnectionManager } from './local.js'

const DOCUMENT = {
  type: 'object', additionalProperties: false, properties: {
    id: { type: 'string', required: true }, sourceId: { type: 'string' },
    filename: { type: 'string' }, status: { type: 'string', enum: ['pending', 'loading', 'extracting', 'pausing', 'paused', 'deleting', 'delete_failed', 'ready', 'failed'] },
    progress: { type: 'integer' }, chunkCount: { type: 'integer' },
    error: { oneOf: [{ type: 'string' }, { type: 'null' }] },
    errorLayer: { oneOf: [{ type: 'string' }, { type: 'null' }] },
    errorStage: { oneOf: [{ type: 'string' }, { type: 'null' }] },
  },
} as const

function documentValue(document: SagDocument) {
  return {
    id: document.id,
    ...(typeof document.sourceId === 'string' ? { sourceId: document.sourceId } : {}),
    ...(typeof document.filename === 'string' ? { filename: document.filename } : {}),
    ...(typeof document.status === 'string' ? { status: document.status } : {}),
    ...(typeof document.progress === 'number' ? { progress: document.progress } : {}),
    ...(typeof document.chunkCount === 'number' ? { chunkCount: document.chunkCount } : {}),
    ...(document.error === null || typeof document.error === 'string' ? { error: document.error } : {}),
    ...(document.errorLayer === null || typeof document.errorLayer === 'string' ? { errorLayer: document.errorLayer } : {}),
    ...(document.errorStage === null || typeof document.errorStage === 'string' ? { errorStage: document.errorStage } : {}),
  }
}

/** Require user approval for irreversible SAG document deletion. */
export function sagDeleteApprovalGate(
  exec: Pick<ToolExecution, 'name'>,
  next: () => Promise<PreToolDecision>,
): Promise<PreToolDecision> {
  if (exec.name !== 'sag_delete_document') return next()
  return Promise.resolve({ kind: 'ask', reason: '删除 SAG 文档后无法恢复' })
}

/** Build local SAG document inspection, reprocessing, and deletion tools. */
export function createDocumentTools(manager: ConnectionManager, config: ResolvedLocalConfig) {
  const list = defineTool({
    name: 'sag_list_documents',
    description: 'List documents and current processing states in a selected local SAG knowledge base.',
    parameters: { source_id: { type: 'string', description: 'Knowledge-base id.' } },
    output: {
      schema: { type: 'object', additionalProperties: false, properties: {
        sourceId: { type: 'string', required: true }, documents: { type: 'array', required: true, items: DOCUMENT },
      } },
      render: (_args, value) => jsonRender(value),
    },
    timeoutMs: config.requestTimeoutMs,
    isConcurrencySafe: () => true,
    async execute(args, exec) {
      return localOperation(manager, exec.signal, async connection => {
        requireToolCapability(connection, 'sag_list_documents')
        const sourceId = await selectSource(connection, args.source_id, exec.signal)
        return { sourceId, documents: (await connection.gateway.listDocuments(sourceId, exec.signal)).map(documentValue) }
      })
    },
    presentCall: args => ({ card: 'generic', title: 'List SAG documents', kind: 'read', rawInput: args.source_id }),
  })

  const get = defineTool({
    name: 'sag_get_document',
    description: 'Get one local SAG document and its current background processing status.',
    parameters: {
      document_id: { type: 'string', required: true, description: 'Document id.' },
      source_id: { type: 'string', description: 'Knowledge-base id.' },
    },
    output: { schema: DOCUMENT, render: (_args, value) => jsonRender(value) },
    timeoutMs: config.requestTimeoutMs,
    isConcurrencySafe: () => true,
    async execute(args, exec) {
      return localOperation(manager, exec.signal, async connection => {
        requireToolCapability(connection, 'sag_get_document')
        const sourceId = await selectSource(connection, args.source_id, exec.signal)
        return documentValue(await connection.gateway.getDocument(sourceId, args.document_id, exec.signal))
      })
    },
    presentCall: args => ({ card: 'generic', title: 'Read SAG document status', kind: 'read', rawInput: args.document_id }),
  })

  const reprocess = defineTool({
    name: 'sag_reprocess_document',
    description: 'Queue asynchronous reprocessing for one local SAG document and return the accepted job state.',
    parameters: {
      document_id: { type: 'string', required: true, description: 'Document id.' },
      source_id: { type: 'string', description: 'Knowledge-base id.' },
    },
    output: { schema: {
      type: 'object', additionalProperties: false, properties: {
        accepted: { type: 'boolean', required: true }, jobId: { type: 'string', required: true },
        documentId: { type: 'string', required: true }, sourceId: { type: 'string', required: true },
        status: { type: 'string', enum: ['queued', 'running', 'paused', 'succeeded', 'failed'], required: true },
        type: { type: 'string', const: 'reprocess_document', required: true },
        progress: { type: 'number' }, attempts: { type: 'integer' },
        error: { oneOf: [{ type: 'string' }, { type: 'null' }] },
      },
    }, render: (_args, value) => jsonRender(value) },
    timeoutMs: config.requestTimeoutMs,
    async execute(args, exec) {
      return localOperation(manager, exec.signal, async connection => {
        requireToolCapability(connection, 'sag_reprocess_document')
        const sourceId = await selectSource(connection, args.source_id, exec.signal)
        const job = await connection.gateway.reprocessDocument(sourceId, args.document_id, exec.signal)
        return {
          accepted: true, jobId: job.id, documentId: job.documentId ?? args.document_id,
          sourceId: job.sourceId ?? sourceId, status: job.status,
          type: job.type,
          ...(job.progress === undefined ? {} : { progress: job.progress }),
          ...(job.attempts === undefined ? {} : { attempts: job.attempts }),
          ...(job.error === undefined ? {} : { error: job.error }),
        }
      })
    },
    presentCall: args => ({ card: 'generic', title: 'Reprocess SAG document', kind: 'execute', rawInput: args.document_id }),
  })

  const remove = defineTool({
    name: 'sag_delete_document',
    description: 'Permanently delete one document from a local SAG knowledge base after dsh user approval.',
    parameters: {
      document_id: { type: 'string', required: true, description: 'Document id.' },
      source_id: { type: 'string', description: 'Knowledge-base id.' },
    },
    output: { schema: {
      type: 'object', additionalProperties: false, properties: {
        deleted: { type: 'boolean', required: true }, documentId: { type: 'string', required: true }, sourceId: { type: 'string', required: true },
      },
    }, render: (_args, value) => jsonRender(value) },
    timeoutMs: config.requestTimeoutMs,
    async execute(args, exec) {
      return localOperation(manager, exec.signal, async connection => {
        requireToolCapability(connection, 'sag_delete_document')
        const sourceId = await selectSource(connection, args.source_id, exec.signal)
        await connection.gateway.deleteDocument(sourceId, args.document_id, exec.signal)
        return { deleted: true, documentId: args.document_id, sourceId }
      })
    },
    presentCall: args => ({ card: 'generic', title: 'Delete SAG document', kind: 'delete', rawInput: args.document_id }),
  })

  return [list, get, reprocess, remove] as const
}
