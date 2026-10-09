import { defineTool } from '@deepseek-ai/dsh-tools'
import { sagEvidenceRef } from '../brand.js'
import type { SagConnectionManager } from '../connection/manager.js'
import type { ResolvedConfig, ResolvedEmbeddedConfig, ResolvedLocalConfig } from '../config.js'
import { decodeLocalEvidenceRef } from '../local/api-client.js'
import { presentReadCall } from '../presentation.js'
import type { SagRuntimeClient } from '../runtime/client.js'
import type { ReadResult } from '../runtime/protocol.js'
import { localInputError, localOperation, requireToolCapability } from './local.js'
import { READ_OUTPUT_SCHEMA, renderRead } from './output.js'

interface ReadOutput {
  title: string
  content: string
  offset: number
  nextOffset?: number
  totalChars: number
  events?: Array<{ id?: string; title?: string; summary?: string; category?: string; rank?: number }>
}

function readResult(value: unknown): ReadOutput {
  if (value === null || typeof value !== 'object' || !('content' in value)) throw new Error('SAG runtime returned the wrong read result type')
  return value as ReadOutput
}

function createEmbeddedReadTool(client: SagRuntimeClient, config: ResolvedEmbeddedConfig) {
  return defineTool({
    name: 'sag_read', description: 'Read a bounded page from one embedded evidence_ref returned by sag_search.',
    parameters: {
      evidence_ref: { type: 'string', required: true, description: 'Opaque reference returned by sag_search.' },
      offset: { type: 'integer', description: 'Unicode character offset, starting at 0.' },
      max_chars: { type: 'integer', description: 'Maximum characters to return.' },
      include_events: { type: 'boolean', description: 'Include bounded related event summaries.' },
    },
    output: { schema: READ_OUTPUT_SCHEMA, render: (_args, value) => [{ type: 'text', text: renderRead(value as ReadResult) }] },
    timeoutMs: config.requestTimeoutMs, isConcurrencySafe: () => true,
    async execute(args, exec) {
      const evidenceRef = sagEvidenceRef(args.evidence_ref)
      const offset = args.offset ?? 0
      const requestedMaxChars = args.max_chars ?? config.maxReadChars
      if (!Number.isInteger(offset) || offset < 0) throw new Error('sag_read offset must not be negative')
      if (!Number.isInteger(requestedMaxChars) || requestedMaxChars < 1) throw new Error('sag_read max_chars must be positive')
      return readResult(await client.request('read', {
        evidenceRef, offset, maxChars: Math.min(requestedMaxChars, config.maxReadChars), includeEvents: args.include_events ?? false,
      }, exec.signal))
    },
    presentCall: args => presentReadCall(args),
  })
}

function createLocalReadTool(manager: Pick<SagConnectionManager, 'ensureConnected'>, config: ResolvedLocalConfig) {
  return defineTool({
    name: 'sag_read', description: 'Read a bounded Unicode page from one local evidence_ref returned by sag_search.',
    parameters: {
      evidence_ref: { type: 'string', required: true, description: 'Opaque reference returned by sag_search.' },
      offset: { type: 'integer', description: 'Unicode character offset, starting at 0.' },
      max_chars: { type: 'integer', description: 'Maximum characters, capped by plugin configuration.' },
    },
    output: { schema: READ_OUTPUT_SCHEMA, render: (_args, value) => [{ type: 'text', text: renderRead(value as ReadResult) }] },
    timeoutMs: config.requestTimeoutMs, isConcurrencySafe: () => true,
    execute(args, exec) {
      return localOperation(manager, exec.signal, async connection => {
        requireToolCapability(connection, 'sag_read')
        const offset = args.offset ?? 0
        const requestedMaxChars = args.max_chars ?? config.maxReadChars
        if (!Number.isInteger(offset) || offset < 0) localInputError('sag_read offset must not be negative')
        if (!Number.isInteger(requestedMaxChars) || requestedMaxChars < 1) localInputError('sag_read max_chars must be positive')
        let evidenceRef: string
        try {
          decodeLocalEvidenceRef(args.evidence_ref)
          evidenceRef = args.evidence_ref
        } catch (error) {
          localInputError(error instanceof Error ? error.message : 'dsh-sag: invalid local evidence reference')
        }
        const chunk = await connection.gateway.read({ evidenceRef }, exec.signal)
        const characters = Array.from(chunk.content)
        const boundedMaxChars = Math.min(requestedMaxChars, config.maxReadChars)
        const content = characters.slice(offset, offset + boundedMaxChars).join('')
        const consumed = Array.from(content).length
        const nextOffset = offset + consumed
        return { title: `${chunk.sourceId}/${chunk.chunkId}`, content, offset,
          ...(nextOffset < characters.length && consumed > 0 ? { nextOffset } : {}), totalChars: characters.length }
      })
    },
    presentCall: args => presentReadCall(args),
  })
}

/** Build the mode-specific bounded evidence reader. */
export function createReadTool(client: SagRuntimeClient | Pick<SagConnectionManager, 'ensureConnected'>, config: ResolvedConfig) {
  return config.mode === 'embedded'
    ? createEmbeddedReadTool(client as SagRuntimeClient, config)
    : createLocalReadTool(client as Pick<SagConnectionManager, 'ensureConnected'>, config)
}
