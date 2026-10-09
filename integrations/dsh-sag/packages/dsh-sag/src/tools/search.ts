import { defineTool, type ValueSchemaSpec } from '@deepseek-ai/dsh-tools'
import type { SagConnectionManager } from '../connection/manager.js'
import type { ResolvedConfig, ResolvedEmbeddedConfig, ResolvedLocalConfig } from '../config.js'
import { presentSearchCall } from '../presentation.js'
import type { SagRuntimeClient } from '../runtime/client.js'
import type { SearchResult } from '../runtime/protocol.js'
import { localInputError, localOperation, requireToolCapability } from './local.js'
import { SEARCH_OUTPUT_SCHEMA, renderSearch } from './output.js'

interface SearchOutput {
  query: string
  evidences: Array<{ evidenceRef: string; namespaceId: string; sourceId: string; title: string; excerpt: string; score?: number }>
}

const LOCAL_SEARCH_OUTPUT_SCHEMA = {
  type: 'object', additionalProperties: false, properties: {
    query: { type: 'string', required: true },
    evidences: { type: 'array', required: true, items: {
      type: 'object', additionalProperties: false, properties: {
        evidenceRef: { type: 'string', required: true }, sourceId: { type: 'string', required: true },
        sourceName: { oneOf: [{ type: 'string' }, { type: 'null' }], required: true },
        chunkId: { type: 'string', required: true }, title: { type: 'string', required: true },
        excerpt: { type: 'string', required: true }, score: { type: 'number', required: true }, rank: { type: 'integer', required: true },
      },
    } },
  },
} as const satisfies ValueSchemaSpec

function renderLocalSearch(value: unknown): string {
  const result = value as { evidences: Array<{ sourceName: string | null; sourceId: string; title: string; excerpt: string; score: number; evidenceRef: string }> }
  if (result.evidences.length === 0) return 'No SAG evidence matched the query.'
  return result.evidences.map((evidence, index) =>
    `${index + 1}. [${evidence.sourceName ?? evidence.sourceId}] ${evidence.title} (score ${evidence.score.toFixed(3)})\n   ${evidence.excerpt}\n   evidence_ref: ${evidence.evidenceRef}`,
  ).join('\n\n')
}

function searchResult(value: unknown): SearchOutput {
  if (value === null || typeof value !== 'object' || !('evidences' in value)) throw new Error('SAG runtime returned the wrong search result type')
  return value as SearchOutput
}

function createEmbeddedSearchTool(client: SagRuntimeClient, config: ResolvedEmbeddedConfig) {
  const allowed = new Set<string>(config.namespaces.map(namespace => namespace.id))
  const labels = new Map(config.namespaces.map(namespace => [namespace.id, namespace.label]))
  return defineTool({
    name: 'sag_search',
    description: 'Search configured embedded SAG namespaces and return evidence_ref values for follow-up sag_read calls.',
    parameters: {
      query: { type: 'string', required: true, description: 'Natural-language or identifier search query.' },
      namespaces: { type: 'array', items: { type: 'string' }, description: 'Optional configured namespace ids.' },
      mode: { type: 'string', enum: ['fast', 'precise'], description: 'Embedded retrieval expansion mode.' },
      limit: { type: 'integer', description: 'Maximum evidence items requested.' },
    },
    output: { schema: SEARCH_OUTPUT_SCHEMA, render: (_args, value) => [{ type: 'text', text: renderSearch(value as unknown as SearchResult, labels) }] },
    timeoutMs: config.requestTimeoutMs,
    isConcurrencySafe: () => true,
    async execute(args, exec) {
      const query = args.query.trim()
      if (!query) throw new Error('sag_search query must not be empty')
      const namespaces = args.namespaces ?? config.namespaces.map(namespace => namespace.id)
      if (namespaces.length === 0 || namespaces.some(namespace => !allowed.has(namespace))) throw new Error('sag_search namespaces must be configured')
      const requestedLimit = args.limit ?? config.maxResults
      if (!Number.isInteger(requestedLimit) || requestedLimit < 1) throw new Error('sag_search limit must be positive')
      return searchResult(await client.request('search', {
        query, namespaces: [...new Set(namespaces)], mode: args.mode ?? config.defaultMode,
        limit: Math.min(requestedLimit, config.maxResults),
      }, exec.signal))
    },
    presentCall: args => presentSearchCall(args),
  })
}

function createLocalSearchTool(manager: Pick<SagConnectionManager, 'ensureConnected'>, config: ResolvedLocalConfig) {
  return defineTool({
    name: 'sag_search',
    description: 'Search one or more local SAG knowledge bases and return opaque evidence_ref values for sag_read.',
    parameters: {
      query: { type: 'string', required: true, description: 'Natural-language or identifier search query.' },
      source_ids: { type: 'array', items: { type: 'string' }, description: 'Optional local SAG knowledge-base ids.' },
      strategy: { type: 'string', enum: ['vector', 'multi', 'multi_es_fast'], description: 'SAG search strategy.' },
      limit: { type: 'integer', description: 'Maximum evidence items, from 1 through 50.' },
    },
    output: { schema: LOCAL_SEARCH_OUTPUT_SCHEMA, render: (_args, value) => [{ type: 'text', text: renderLocalSearch(value) }] },
    timeoutMs: config.requestTimeoutMs,
    isConcurrencySafe: () => true,
    execute(args, exec) {
      return localOperation(manager, exec.signal, async connection => {
        requireToolCapability(connection, 'sag_search')
        const query = args.query.trim()
        if (!query) localInputError('sag_search query must not be empty')
        if (args.limit !== undefined && (!Number.isInteger(args.limit) || args.limit < 1 || args.limit > 50)) localInputError('sag_search limit must be an integer from 1 to 50')
        const sourceIds = args.source_ids?.map(value => value.trim()).filter(Boolean)
        if (args.source_ids !== undefined && sourceIds?.length === 0) localInputError('sag_search source_ids must contain a knowledge-base id')
        const result = await connection.gateway.search({
          query,
          ...(sourceIds === undefined ? {} : { sourceIds: [...new Set(sourceIds)] }),
          ...(args.limit === undefined ? {} : { topK: args.limit }),
          ...(args.strategy === undefined ? {} : { strategy: args.strategy }),
        }, exec.signal)
        return { query: result.query, evidences: result.evidences.map(evidence => ({
          evidenceRef: evidence.evidenceRef, sourceId: evidence.sourceId, sourceName: evidence.sourceName, chunkId: evidence.chunkId,
          title: evidence.heading || evidence.sourceName || evidence.chunkId, excerpt: evidence.content, score: evidence.score, rank: evidence.rank,
        })) }
      })
    },
    presentCall: args => presentSearchCall(args),
  })
}

/** Build the mode-specific model-facing SAG search tool. */
export function createSearchTool(client: SagRuntimeClient | Pick<SagConnectionManager, 'ensureConnected'>, config: ResolvedConfig) {
  return config.mode === 'embedded'
    ? createEmbeddedSearchTool(client as SagRuntimeClient, config)
    : createLocalSearchTool(client as Pick<SagConnectionManager, 'ensureConnected'>, config)
}
