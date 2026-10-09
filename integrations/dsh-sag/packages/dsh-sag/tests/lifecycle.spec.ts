import { PassThrough } from 'node:stream'
import { Context, Service } from '@deepseek-ai/cordis'
import { createVolatile, updateVolatile } from '@deepseek-ai/cosmokit'
import type { Agent } from '@deepseek-ai/dsh-agent'
import type { CredentialKey, CredentialRecord } from '@deepseek-ai/dsh-credentials'
import type { FileSystem } from '@deepseek-ai/dsh-fs'
import { ToolCallId } from '@deepseek-ai/dsh-llm'
import SubprocessRuntime, { type SubprocessHandle, type SubprocessSpawnSpec } from '@deepseek-ai/dsh-subprocess'
import SystemPrompt from '@deepseek-ai/dsh-system-prompt'
import ToolRuntime, { defineTool } from '@deepseek-ai/dsh-tools'
import { describe, expect, it, vi } from 'vitest'
import * as DshSag from '../src/index.ts'
import type { SagConnectionReport } from '../src/connection/manager.ts'
import type { SagGateway } from '../src/local/gateway.ts'
import { createDocumentTools, sagDeleteApprovalGate } from '../src/tools/documents.ts'
import { resolveConfig } from '../src/config.ts'

class FakeSubprocess extends SubprocessRuntime {
  spawned = 0
  terminated = 0
  engineVersion = '0.14.0'

  async resolveExecutable(): Promise<string> {
    return '/fake/python'
  }

  spawn(_spec: SubprocessSpawnSpec): SubprocessHandle {
    this.spawned += 1
    const stdin = new PassThrough()
    const stdout = new PassThrough()
    let finish!: (value: { exitCode: number | null; signal: NodeJS.Signals | null }) => void
    const done = new Promise<{ exitCode: number | null; signal: NodeJS.Signals | null }>(resolve => { finish = resolve })
    stdin.on('data', chunk => {
      for (const line of String(chunk).trim().split('\n')) {
        const value = JSON.parse(line)
        if (value.method === 'initialize') {
          stdout.write(`${JSON.stringify({ jsonrpc: '2.0', id: value.id, result: {
            protocolVersion: '1.0', engineVersion: this.engineVersion, health: 'available',
            evidenceRead: true, namespaces: ['product-docs'],
          } })}\n`)
        }
        if (value.method === 'shutdown') stdout.write(`${JSON.stringify({ jsonrpc: '2.0', id: value.id, result: {} })}\n`)
      }
    })
    stdin.on('finish', () => finish({ exitCode: 0, signal: null }))
    return {
      pid: 1, stdin, stdout, stderr: undefined, collected: {}, done,
      terminate: () => { this.terminated += 1; finish({ exitCode: null, signal: 'SIGTERM' }) },
      waitForExit: async () => { await done; return true },
    }
  }
}

async function base() {
  const ctx = new Context()
  await ctx.plugin(SystemPrompt)
  await ctx.plugin(ToolRuntime)
  await ctx.plugin(FakeSubprocess)
  return ctx
}

const config = {
  mode: 'embedded' as const,
  pythonCommand: 'python3', envFile: '/config/sag.env', cwd: '/runtime',
  namespaces: [{ id: 'product-docs', label: '产品文档' }],
}

// A focused volatile-config fixture; actual SettingsForms/profile persistence is
// covered by profile.spec.ts against the published host implementation.
class MemorySettingsProvider extends Service {
  constructor(private readonly ownerContext: Context) { super(ownerContext, 'settings') }
  async update(_namespace: string, patch: { connection: unknown }): Promise<void> {
    for (const fiber of this.ownerContext.registry.get(DshSag)?.fibers ?? []) {
      updateVolatile(fiber.config.connection, createVolatile(patch.connection))
    }
  }
}

function localServices(ctx: Context): void {
  const records = new Map<CredentialKey, CredentialRecord>()
  ctx.provide('credentials', {
    readRecord: (key: CredentialKey) => Promise.resolve(records.get(key)),
    async modifyRecord(key: CredentialKey, mutate: (value: CredentialRecord | undefined) => Promise<CredentialRecord | undefined>) {
      const next = await mutate(records.get(key))
      if (next === undefined) records.delete(key)
      else records.set(key, next)
      return next
    },
    deleteRecord(key: CredentialKey) { records.delete(key); return Promise.resolve() },
  } as unknown as Context['credentials'])
  ctx.provide('fs', {
    resolve: () => Promise.reject(new Error('missing test file')),
    readText: () => Promise.reject(new Error('missing test file')),
  } as unknown as FileSystem)
}

function fakeAgent(): Agent {
  return { session: { events: [{ type: 'turn/start' }], append: () => ({}) } } as unknown as Agent
}

function deleteGateway(onDelete: () => void): SagGateway {
  return {
    health: () => Promise.resolve(), ready: () => Promise.resolve(),
    capabilities: () => Promise.reject(new Error('unused')), listSources: () => Promise.resolve([{ id: 'source', name: 'Source' }]),
    createSource: () => Promise.reject(new Error('unused')), search: () => Promise.reject(new Error('unused')),
    read: () => Promise.reject(new Error('unused')), listDocuments: () => Promise.reject(new Error('unused')),
    getDocument: () => Promise.reject(new Error('unused')), uploadFile: () => Promise.reject(new Error('unused')),
    ingestText: () => Promise.reject(new Error('unused')), reprocessDocument: () => Promise.reject(new Error('unused')),
    deleteDocument: () => { onDelete(); return Promise.resolve({ deleted: true }) },
  }
}

function managerFor(gateway: SagGateway) {
  const report: SagConnectionReport = {
    status: 'ready', health: true, ready: true, sourceCount: 1,
    descriptor: {
      schemaVersion: 1, name: 'SAG', apiUrl: 'http://127.0.0.1:8000/api/v1',
      mcpUrl: 'http://127.0.0.1:8000/mcp/', accessToken: 'token', defaultSourceId: 'source',
    },
    capabilities: { schemaVersion: 1, capabilities: ['documents.delete'], upload: { maxMb: 1, extensions: ['md'] }, defaultSourceId: 'source' },
    gateway,
  }
  return { ensureConnected: () => Promise.resolve(report) }
}

describe('Cordis lifecycle', () => {
  it('defaults to local mode, starts no sidecar, registers eleven tools, and disposes every contribution', async () => {
    const ctx = new Context()
    await ctx.plugin(SystemPrompt)
    await ctx.plugin(ToolRuntime)
    await ctx.plugin(MemorySettingsProvider)
    await ctx.plugin(FakeSubprocess)
    localServices(ctx)

    const fork = ctx.plugin(DshSag, {})
    await fork

    expect(ctx.subprocess.spawned).toBe(0)
    expect(ctx.subprocess.terminated).toBe(0)
    expect(ctx.tools.schemas().map(tool => tool.name)).toEqual([
      'sag_status', 'sag_list_sources', 'sag_create_source', 'sag_search', 'sag_read',
      'sag_list_documents', 'sag_get_document', 'sag_reprocess_document', 'sag_delete_document',
      'sag_upload_file', 'sag_ingest_text',
    ])
    expect((await ctx.systemPrompt.assemble()).sections.some(section => section.name === 'tool:dsh-sag')).toBe(true)

    let unrelatedCalls = 0
    ctx.tools.register(defineTool({
      name: 'unrelated', description: 'Unrelated test tool for waterfall delegation.', parameters: {},
      output: { schema: { type: 'object', additionalProperties: false, properties: {} }, render: () => [] },
      execute: () => { unrelatedCalls += 1; return Promise.resolve({}) },
    }))
    await expect(ctx.tools.execute({
      callId: ToolCallId('unrelated'), name: 'unrelated', arguments: {}, signal: new AbortController().signal,
    })).resolves.toMatchObject({ isError: false })
    expect(unrelatedCalls).toBe(1)

    await fork.dispose()
    expect(ctx.tools.get('sag_search')).toBeUndefined()
    expect(ctx.tools.get('sag_delete_document')).toBeUndefined()
    expect((await ctx.systemPrompt.assemble()).sections.some(section => section.name === 'tool:dsh-sag')).toBe(false)
  })

  it('rejects invalid mode before contributing any SAG tools or prompt section', async () => {
    const ctx = await base()
    try {
      await expect(ctx.plugin(DshSag, { mode: 'invalid' } as never)).rejects.toThrow(/mode/)
      expect(ctx.tools.schemas()).toEqual([])
      expect((await ctx.systemPrompt.assemble()).sections.some(section => section.name === 'tool:dsh-sag')).toBe(false)
      expect(ctx.subprocess.spawned).toBe(0)
    } finally { await ctx.fiber.dispose() }
  })

  it('fails closed before delete dispatch and allows exactly one approved execution through ToolRuntime', async () => {
    const ctx = new Context()
    await ctx.plugin(SystemPrompt)
    await ctx.plugin(ToolRuntime)
    let deletes = 0
    const local = resolveConfig({})
    if (local.mode !== 'local') throw new Error('expected local config')
    ctx.tools.register(createDocumentTools(managerFor(deleteGateway(() => { deletes += 1 })), local)[3])
    ctx.on('tools/pre-execute', sagDeleteApprovalGate)
    const input = {
      name: 'sag_delete_document', arguments: { document_id: 'doc' }, signal: new AbortController().signal,
    }

    const withoutApproval = await ctx.tools.execute({ ...input, callId: ToolCallId('delete-no-approval') })
    expect(withoutApproval.isError).toBe(true)
    expect(deletes).toBe(0)

    const rejectedProvider = ctx.provide('approval', { request: vi.fn().mockResolvedValue('rejected') } as never)
    const rejected = await ctx.tools.execute({ ...input, callId: ToolCallId('delete-rejected'), agent: fakeAgent() })
    expect(rejected.isError).toBe(true)
    expect(deletes).toBe(0)
    await rejectedProvider()

    ctx.provide('approval', { request: vi.fn().mockResolvedValue('allowed-once') } as never)
    const allowed = await ctx.tools.execute({ ...input, callId: ToolCallId('delete-allowed'), agent: fakeAgent() })
    expect(allowed).toMatchObject({ isError: false, value: { deleted: true } })
    expect(deletes).toBe(1)
  })

  it('registers exactly two tools after readiness and removes them on unload', async () => {
    const ctx = await base()
    const fork = ctx.plugin(DshSag, config)
    expect(ctx.tools.get('sag_search')).toBeUndefined()
    await fork

    expect(ctx.tools.get('sag_search')?.name).toBe('sag_search')
    expect(ctx.tools.get('sag_read')?.name).toBe('sag_read')
    expect((await ctx.systemPrompt.assemble()).sections.some(section => section.name === 'tool:dsh-sag')).toBe(true)

    await fork.dispose()
    expect(ctx.tools.get('sag_search')).toBeUndefined()
    expect(ctx.tools.get('sag_read')).toBeUndefined()
  })

  it('rolls back without registering tools when readiness fails', async () => {
    const ctx = await base()
    ctx.subprocess.engineVersion = '0.9.0'
    const fork = ctx.plugin(DshSag, config)

    await expect(fork).rejects.toThrow(/0\.14\.0/)
    expect(ctx.tools.get('sag_search')).toBeUndefined()
    expect(ctx.subprocess.terminated).toBe(1)
  })
})
