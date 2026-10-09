import { defineTool } from '@deepseek-ai/dsh-tools'
import type { ResolvedLocalConfig } from '../config.js'
import { jsonRender, localInputError, localOperation, requireToolCapability, selectSource, type ConnectionManager } from './local.js'

const INGEST_OUTPUT = {
  type: 'object', additionalProperties: false, properties: {
    accepted: { type: 'boolean', required: true }, documentId: { type: 'string', required: true },
    sourceId: { type: 'string', required: true }, status: { type: 'string', required: true },
  },
} as const

/** Build direct text ingestion for one local SAG knowledge base. */
export function createIngestTextTool(manager: ConnectionManager, config: ResolvedLocalConfig) {
  return defineTool({
    name: 'sag_ingest_text',
    description: 'Submit text to a SAG knowledge base and return the accepted document processing status.',
    parameters: {
      text: { type: 'string', required: true, description: 'Text to ingest.' },
      title: { type: 'string', description: 'Optional document title.' },
      source_id: { type: 'string', description: 'Target knowledge-base id; omitted only when a default or single source exists.' },
    },
    output: { schema: INGEST_OUTPUT, render: (_args, value) => jsonRender(value) },
    timeoutMs: config.requestTimeoutMs,
    async execute(args, exec) {
      return localOperation(manager, exec.signal, async connection => {
        requireToolCapability(connection, 'sag_ingest_text')
        const text = args.text.trim()
        if (!text) localInputError('sag_ingest_text text must not be empty')
        const sourceId = await selectSource(connection, args.source_id, exec.signal)
        const document = await connection.gateway.ingestText({ sourceId, text, ...(args.title === undefined ? {} : { title: args.title }) }, exec.signal)
        return { accepted: true, documentId: document.id, sourceId, status: document.status ?? 'pending' }
      })
    },
    presentCall: args => ({ card: 'generic', title: 'Ingest text into SAG', kind: 'edit', rawInput: args.title ?? `${args.text.slice(0, 80)}${args.text.length > 80 ? '…' : ''}` }),
  })
}
