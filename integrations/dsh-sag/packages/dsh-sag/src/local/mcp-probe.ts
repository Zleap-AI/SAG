import { Client } from '@modelcontextprotocol/sdk/client/index.js'
import { StreamableHTTPClientTransport } from '@modelcontextprotocol/sdk/client/streamableHttp.js'
import type { Transport } from '@modelcontextprotocol/sdk/shared/transport.js'
import type { SagConnectionDescriptor } from '../connection/types.js'

/** Successful MCP compatibility result; no MCP tools are registered in dsh. */
export interface McpProbeResult {
  readonly tools: readonly string[]
  readonly readTool?: 'read' | 'get_chunk'
}

/** One temporary MCP connection, injectable so lifecycle behavior is observable. */
export interface McpProbeSession {
  connect(signal: AbortSignal): Promise<void>
  listTools(signal: AbortSignal): Promise<readonly string[]>
  close(): Promise<void>
}

/** Factory for a temporary MCP probe session. */
export type McpProbeSessionFactory = (descriptor: SagConnectionDescriptor) => McpProbeSession

/** A reachable MCP endpoint whose advertised tools do not satisfy dsh-sag. */
export class McpProbeIncompatibleError extends Error {
  /** @param message - redacted compatibility failure. */
  constructor(message: string) {
    super(`dsh-sag: SAG MCP is incompatible: ${message}`)
    this.name = 'McpProbeIncompatibleError'
  }
}

/** A temporary transport, initialization, listing, or closure failure. */
export class McpProbeUnreachableError extends Error {
  /** @param message - redacted connection failure. */
  constructor(message: string) {
    super(`dsh-sag: SAG MCP is unreachable: ${message}`)
    this.name = 'McpProbeUnreachableError'
  }
}

function redacted(error: unknown, token: string): string {
  const message = error instanceof Error ? error.message : String(error)
  return message.split(token).join('<redacted>')
}

function productionSession(descriptor: SagConnectionDescriptor): McpProbeSession {
  const client = new Client({ name: 'dsh-sag-probe', version: '1.0.0' })
  const transport = new StreamableHTTPClientTransport(new URL(descriptor.mcpUrl), {
    requestInit: { headers: { Authorization: `Bearer ${descriptor.accessToken}` } },
  }) as Transport
  let connected = false
  return {
    async connect(signal) {
      await client.connect(transport, { signal })
      connected = true
    },
    async listTools(signal) {
      const response = await client.listTools({}, { signal })
      return response.tools.map(tool => tool.name)
    },
    async close() {
      if (connected) await client.close()
      else await transport.close()
    },
  }
}

function compatible(names: readonly string[], advertisedCapabilities?: readonly string[]): McpProbeResult {
  const tools = [...names].sort()
  const required = advertisedCapabilities === undefined ? ['list_sources', 'search'] : [
    ...(advertisedCapabilities.includes('knowledge.search') ? ['search'] : []),
  ]
  for (const tool of required) if (!tools.includes(tool)) throw new McpProbeIncompatibleError(`required tool ${tool} is missing`)
  const readTool = tools.includes('read') ? 'read' : tools.includes('get_chunk') ? 'get_chunk' : undefined
  if ((advertisedCapabilities === undefined || advertisedCapabilities.includes('knowledge.read')) && readTool === undefined) {
    throw new McpProbeIncompatibleError('required tool read or get_chunk is missing')
  }
  return { tools, ...(readTool === undefined ? {} : { readTool }) }
}

/** Initialize SAG MCP, verify retrieval tools, and require successful temporary-session closure. */
export async function probeMcp(
  descriptor: SagConnectionDescriptor,
  signal: AbortSignal,
  factory: McpProbeSessionFactory = productionSession,
  advertisedCapabilities?: readonly string[],
): Promise<McpProbeResult> {
  if (signal.aborted) throw signal.reason
  const session = factory(descriptor)
  let result: McpProbeResult | undefined
  let failure: unknown
  try {
    await session.connect(signal)
    result = compatible(await session.listTools(signal), advertisedCapabilities)
  } catch (error) {
    failure = error instanceof McpProbeIncompatibleError
      ? error
      : new McpProbeUnreachableError(redacted(error, descriptor.accessToken))
  }

  try {
    await session.close()
  } catch (error) {
    const closeFailure = new McpProbeUnreachableError(`close failed: ${redacted(error, descriptor.accessToken)}`)
    if (failure === undefined) failure = closeFailure
  }

  if (signal.aborted) throw signal.reason
  if (failure !== undefined) throw failure
  if (result === undefined) throw new McpProbeUnreachableError('probe completed without a result')
  return result
}
