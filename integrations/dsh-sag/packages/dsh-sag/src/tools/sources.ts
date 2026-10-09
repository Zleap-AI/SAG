import { defineTool } from '@deepseek-ai/dsh-tools'
import type { ResolvedLocalConfig } from '../config.js'
import type { SagSource } from '../local/api-client.js'
import { jsonRender, localInputError, localOperation, requireToolCapability, type ConnectionManager } from './local.js'

const SOURCE = {
  type: 'object', additionalProperties: false, properties: {
    id: { type: 'string', required: true }, name: { type: 'string', required: true },
    description: { type: 'string' }, status: { type: 'string' },
    documentCount: { type: 'integer' }, chunkCount: { type: 'integer' },
  },
} as const

function sourceValue(source: SagSource) {
  return {
    id: source.id, name: source.name,
    ...(typeof source.description === 'string' ? { description: source.description } : {}),
    ...(typeof source.status === 'string' ? { status: source.status } : {}),
    ...(typeof source.documentCount === 'number' ? { documentCount: source.documentCount } : {}),
    ...(typeof source.chunkCount === 'number' ? { chunkCount: source.chunkCount } : {}),
  }
}

/** Build source-list and source-creation tools for local SAG. */
export function createSourceTools(manager: ConnectionManager, config: ResolvedLocalConfig) {
  const list = defineTool({
    name: 'sag_list_sources',
    description: 'List local SAG knowledge bases with stable ids for search, upload, and document operations.',
    parameters: {},
    output: {
      schema: { type: 'object', additionalProperties: false, properties: { sources: { type: 'array', required: true, items: SOURCE } } },
      render: (_args, value) => jsonRender(value),
    },
    timeoutMs: config.requestTimeoutMs,
    isConcurrencySafe: () => true,
    async execute(_args, exec) {
      return localOperation(manager, exec.signal, async connection => {
        requireToolCapability(connection, 'sag_list_sources')
        return { sources: (await connection.gateway.listSources(exec.signal)).map(sourceValue) }
      })
    },
    presentCall: () => ({ card: 'generic', title: 'List SAG knowledge bases', kind: 'read' }),
  })
  const create = defineTool({
    name: 'sag_create_source',
    description: 'Create a local SAG knowledge base that can receive uploaded files or directly ingested text.',
    parameters: {
      name: { type: 'string', required: true, description: 'Knowledge-base name.' },
      description: { type: 'string', description: 'Optional description.' },
    },
    output: { schema: SOURCE, render: (_args, value) => jsonRender(value) },
    timeoutMs: config.requestTimeoutMs,
    async execute(args, exec) {
      return localOperation(manager, exec.signal, async connection => {
        requireToolCapability(connection, 'sag_create_source')
        const name = args.name.trim()
        if (!name) localInputError('sag_create_source name must not be empty')
        return sourceValue(await connection.gateway.createSource({ name, ...(args.description === undefined ? {} : { description: args.description }) }, exec.signal))
      })
    },
    presentCall: args => ({ card: 'generic', title: 'Create SAG knowledge base', kind: 'edit', rawInput: args.name }),
  })
  return [list, create] as const
}
