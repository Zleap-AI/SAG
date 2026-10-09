import { defineTool } from '@deepseek-ai/dsh-tools'
import type { ResolvedLocalConfig } from '../config.js'
import { jsonRender, localOperation, type ConnectionManager } from './local.js'

const STATUS_OUTPUT = {
  type: 'object', additionalProperties: false, properties: {
    status: { type: 'string', required: true },
    name: { type: 'string', required: true },
    sourceCount: { type: 'integer', required: true },
    capabilities: { type: 'array', required: true, items: { type: 'string' } },
  },
} as const

/** Build a read-only summary of the current local SAG connection. */
export function createStatusTool(manager: ConnectionManager, config: ResolvedLocalConfig) {
  return defineTool({
    name: 'sag_status',
    description: 'Check the local SAG connection and report its public capabilities without exposing credentials.',
    parameters: {},
    output: { schema: STATUS_OUTPUT, render: (_args, value) => jsonRender(value) },
    timeoutMs: config.requestTimeoutMs,
    isConcurrencySafe: () => true,
    async execute(_args, exec) {
      return localOperation(manager, exec.signal, async connection => {
        return { status: 'ready', name: connection.descriptor.name, sourceCount: connection.sourceCount, capabilities: [...connection.capabilities.capabilities] }
      })
    },
    presentCall: () => ({ card: 'generic', title: 'Check SAG status', kind: 'fetch' }),
  })
}
