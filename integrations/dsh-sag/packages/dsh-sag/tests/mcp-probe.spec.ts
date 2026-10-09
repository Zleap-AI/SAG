import { createServer, type IncomingMessage, type Server, type ServerResponse } from 'node:http'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js'
import { StreamableHTTPServerTransport } from '@modelcontextprotocol/sdk/server/streamableHttp.js'
import type { Transport } from '@modelcontextprotocol/sdk/shared/transport.js'
import type { SagConnectionDescriptor } from '../src/connection/types.js'
import {
  McpProbeIncompatibleError,
  McpProbeUnreachableError,
  probeMcp,
  type McpProbeSession,
} from '../src/local/mcp-probe.ts'

const servers: Server[] = []

async function fixture(toolNames: readonly string[]): Promise<{ descriptor: SagConnectionDescriptor; seenAuth: string[] }> {
  const seenAuth: string[] = []
  async function handle(req: IncomingMessage, res: ServerResponse): Promise<void> {
    seenAuth.push(req.headers.authorization ?? '')
    const server = new McpServer({ name: 'sag-fixture', version: '1.0.0' }, { capabilities: { tools: {} } })
    for (const name of toolNames) {
      server.registerTool(name, { description: name, inputSchema: {} }, async () => ({ content: [{ type: 'text', text: name }] }))
    }
    const transport = new StreamableHTTPServerTransport({})
    res.on('close', () => { void transport.close(); void server.close() })
    await server.connect(transport as Transport)
    await transport.handleRequest(req, res)
  }
  const http = createServer((req, res) => { void handle(req, res).catch(error => res.writeHead(500).end(String(error))) })
  servers.push(http)
  const listening: PromiseWithResolvers<void> = Promise.withResolvers()
  http.listen(0, '127.0.0.1', listening.resolve)
  await listening.promise
  const address = http.address()
  if (address === null || typeof address === 'string') throw new Error('expected TCP address')
  return {
    descriptor: {
      schemaVersion: 1, name: 'SAG', apiUrl: `http://127.0.0.1:${address.port}/api/v1`,
      mcpUrl: `http://127.0.0.1:${address.port}/mcp`, accessToken: 'sag_local_probe_secret',
    },
    seenAuth,
  }
}

afterEach(async () => {
  await Promise.all(servers.splice(0).map(server => new Promise<void>(resolve => server.close(() => resolve()))))
})

describe('probeMcp', () => {
  it('initializes, lists the required SAG tools, sends bearer auth, and closes', async () => {
    const connection = await fixture(['list_sources', 'search', 'read'])
    const result = await probeMcp(connection.descriptor, new AbortController().signal)

    expect(result).toEqual({ tools: ['list_sources', 'read', 'search'], readTool: 'read' })
    expect(connection.seenAuth).not.toHaveLength(0)
    expect(connection.seenAuth.every(header => header === 'Bearer sag_local_probe_secret')).toBe(true)
  })

  it('accepts get_chunk as the read capability and rejects incompatible tool sets without leaking tokens', async () => {
    const compatible = await fixture(['list_sources', 'search', 'get_chunk'])
    await expect(probeMcp(compatible.descriptor, new AbortController().signal)).resolves.toMatchObject({ readTool: 'get_chunk' })

    const incompatible = await fixture(['list_sources'])
    const error = await probeMcp(incompatible.descriptor, new AbortController().signal).catch((caught: unknown) => caught)
    expect(String(error)).toMatch(/search/)
    expect(String(error)).not.toContain(incompatible.descriptor.accessToken)
    expect(error).toBeInstanceOf(McpProbeIncompatibleError)
  })

  it.each([
    ['success', ['list_sources', 'search', 'read'], undefined],
    ['missing tools', ['list_sources'], McpProbeIncompatibleError],
  ] as const)('closes the client session after %s', async (_label, tools, ErrorType) => {
    const session: McpProbeSession = {
      connect: vi.fn(async () => undefined),
      listTools: vi.fn(async () => tools),
      close: vi.fn(async () => undefined),
    }
    const factory = vi.fn(() => session)
    const operation = probeMcp({
      schemaVersion: 1, name: 'SAG', apiUrl: 'http://127.0.0.1:8000/api/v1',
      mcpUrl: 'http://127.0.0.1:8000/mcp', accessToken: 'secret',
    }, new AbortController().signal, factory)

    if (ErrorType === undefined) await expect(operation).resolves.toMatchObject({ readTool: 'read' })
    else await expect(operation).rejects.toBeInstanceOf(ErrorType)
    expect(session.close).toHaveBeenCalledOnce()
  })

  it('closes after connection failure and classifies transport failure as unreachable', async () => {
    const session: McpProbeSession = {
      connect: vi.fn(async () => { throw new Error('ECONNREFUSED') }),
      listTools: vi.fn(),
      close: vi.fn(async () => undefined),
    }
    const operation = probeMcp({
      schemaVersion: 1, name: 'SAG', apiUrl: 'http://127.0.0.1:8000/api/v1',
      mcpUrl: 'http://127.0.0.1:8000/mcp', accessToken: 'secret',
    }, new AbortController().signal, () => session)

    await expect(operation).rejects.toBeInstanceOf(McpProbeUnreachableError)
    expect(session.close).toHaveBeenCalledOnce()
    expect(session.listTools).not.toHaveBeenCalled()
  })

  it('reports a close failure instead of returning a successful probe', async () => {
    const session: McpProbeSession = {
      connect: vi.fn(async () => undefined),
      listTools: vi.fn(async () => ['list_sources', 'search', 'read']),
      close: vi.fn(async () => { throw new Error('close failed') }),
    }

    await expect(probeMcp({
      schemaVersion: 1, name: 'SAG', apiUrl: 'http://127.0.0.1:8000/api/v1',
      mcpUrl: 'http://127.0.0.1:8000/mcp', accessToken: 'secret',
    }, new AbortController().signal, () => session)).rejects.toMatchObject({
      name: 'McpProbeUnreachableError', message: expect.stringMatching(/close failed/),
    })
  })
})
