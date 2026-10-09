import type { ValueSchemaSpec } from '@deepseek-ai/dsh-tools'
import type { ReadResult, SearchResult } from '../runtime/protocol.js'

export const SEARCH_OUTPUT_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  properties: {
    query: { type: 'string', required: true },
    evidences: {
      type: 'array',
      required: true,
      items: {
        type: 'object',
        additionalProperties: false,
        properties: {
          evidenceRef: { type: 'string', required: true },
          namespaceId: { type: 'string', required: true },
          sourceId: { type: 'string', required: true },
          title: { type: 'string', required: true },
          excerpt: { type: 'string', required: true },
          score: { type: 'number' },
        },
      },
    },
  },
} as const satisfies ValueSchemaSpec

export const READ_OUTPUT_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  properties: {
    title: { type: 'string', required: true },
    content: { type: 'string', required: true },
    offset: { type: 'integer', required: true },
    nextOffset: { type: 'integer' },
    totalChars: { type: 'integer', required: true },
    events: {
      type: 'array',
      items: {
        type: 'object',
        additionalProperties: false,
        properties: {
          id: { type: 'string' },
          title: { type: 'string' },
          summary: { type: 'string' },
          category: { type: 'string' },
          rank: { type: 'integer' },
        },
      },
    },
  },
} as const satisfies ValueSchemaSpec

export function renderSearch(result: SearchResult, labels: ReadonlyMap<string, string>): string {
  if (result.evidences.length === 0) return 'No SAG evidence matched the query.'
  return result.evidences.map((evidence, index) => {
    const label = labels.get(evidence.namespaceId) ?? evidence.namespaceId
    const score = evidence.score === undefined ? '' : ` (score ${evidence.score.toFixed(3)})`
    return `${index + 1}. [${label}] ${evidence.title}${score}\n   ${evidence.excerpt}\n   evidence_ref: ${evidence.evidenceRef}`
  }).join('\n\n')
}

export function renderRead(result: ReadResult): string {
  const events = result.events?.length
    ? `\n\nRelated events:\n${result.events.map(event => `- ${event.title ?? event.id ?? 'event'}${event.summary ? `: ${event.summary}` : ''}`).join('\n')}`
    : ''
  const continuation = result.nextOffset === undefined
    ? ''
    : `\n\nContinue with sag_read using the same evidence_ref and offset=${result.nextOffset}.`
  return `# ${result.title}\n\n${result.content}${events}${continuation}`
}
