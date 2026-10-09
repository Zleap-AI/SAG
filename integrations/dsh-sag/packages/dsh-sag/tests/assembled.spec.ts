import { readFile } from 'node:fs/promises'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { Context, Service } from '@deepseek-ai/cordis'
import { createVolatile, updateVolatile } from '@deepseek-ai/cosmokit'
import type { CredentialKey, CredentialRecord } from '@deepseek-ai/dsh-credentials'
import type { FileSystem } from '@deepseek-ai/dsh-fs'
import { ToolCallId } from '@deepseek-ai/dsh-llm'
import LocalSubprocessRuntime from '@deepseek-ai/dsh-subprocess-local'
import SystemPrompt from '@deepseek-ai/dsh-system-prompt'
import ToolRuntime from '@deepseek-ai/dsh-tools'
import { afterEach, describe, expect, it } from 'vitest'
import * as DshSag from '../src/index.ts'

const root = resolve(dirname(fileURLToPath(import.meta.url)), '../../..')
const fakeRoot = resolve(root, 'examples/local/fake-runtime')
const previousPythonPath = process.env.PYTHONPATH

interface FakeSagServer {
  readonly origin: string
  readonly requests: readonly string[]
  close(): Promise<void>
}

interface FakeSagModule {
  startFakeSagServer(options?: {
    readonly afterListen?: (origin: string) => Promise<void>
    readonly capabilities?: readonly string[]
    readonly mcpTools?: readonly string[]
    readonly searchWithoutUpload?: boolean
    readonly disableSources?: boolean
  }): Promise<FakeSagServer>
  withFakeSagServer<T>(run: (server: FakeSagServer) => Promise<T>): Promise<T>
}

interface Disposable {
  dispose(): Promise<void>
}

async function disposeAll(resources: readonly Disposable[]): Promise<void> {
  const failures: unknown[] = []
  for (const resource of [...resources].reverse()) {
    try {
      await resource.dispose()
    } catch (error) {
      failures.push(error)
    }
  }
  if (failures.length === 1) throw failures[0]
  if (failures.length > 1) throw new AggregateError(failures, 'assembled fixture cleanup failed')
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

function provideLocalServices(ctx: Context, uploadPath: string, bytes: Uint8Array): void {
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
    async resolve(path: string) { return { targetKey: path as never, displayPath: path } },
    async readText() { throw new Error('connection file is absent') },
    async stat(target: { readonly displayPath: string }) {
      if (target.displayPath !== uploadPath) return undefined
      return { type: 'file', size: bytes.length }
    },
    async readBytes(target: { readonly displayPath: string }) {
      if (target.displayPath !== uploadPath) throw new Error('upload fixture is absent')
      return bytes
    },
  } as unknown as FileSystem)
}

function value<T>(result: { readonly value?: unknown }): T {
  return result.value as T
}

afterEach(() => {
  if (previousPythonPath === undefined) delete process.env.PYTHONPATH
  else process.env.PYTHONPATH = previousPythonPath
})

describe('assembled keyless plugin', () => {
  it('matches public probe authentication and closes after setup failure or repeated close', async () => {
    const fakeModule = await import('../../../examples/local/fake-sag-server.mjs') as FakeSagModule
    let owned: FakeSagServer | undefined
    const setupFailure = new Error('assembled setup failed')

    await expect(fakeModule.withFakeSagServer(async server => {
      owned = server
      await expect(fetch(`${server.origin}/api/v1/system/health`)).resolves.toMatchObject({ status: 200 })
      await expect(fetch(`${server.origin}/api/v1/system/ready`)).resolves.toMatchObject({ status: 200 })
      await expect(fetch(`${server.origin}/api/v1/system/dsh`)).resolves.toMatchObject({ status: 401 })
      await expect(fetch(`${server.origin}/mcp/`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ jsonrpc: '2.0', id: 1, method: 'initialize', params: { protocolVersion: '2024-11-05' } }),
      })).resolves.toMatchObject({ status: 401 })
      throw setupFailure
    })).rejects.toBe(setupFailure)

    if (owned === undefined) throw new Error('fake SAG was not started')
    await expect(fetch(`${owned.origin}/api/v1/system/health`)).rejects.toThrow()
    await expect(owned.close()).resolves.toBeUndefined()
    await expect(owned.close()).resolves.toBeUndefined()

    let startupOrigin: string | undefined
    const startupFailure = new Error('fake SAG startup failed')
    await expect(fakeModule.startFakeSagServer({
      async afterListen(origin) {
        startupOrigin = origin
        throw startupFailure
      },
    })).rejects.toBe(startupFailure)
    if (startupOrigin === undefined) throw new Error('fake SAG did not reach the startup failure seam')
    await expect(fetch(`${startupOrigin}/api/v1/system/health`)).rejects.toThrow()
  })

  it('attempts every acquired disposer after a plugin disposal failure', async () => {
    const fakeModule = await import('../../../examples/local/fake-sag-server.mjs') as FakeSagModule
    const calls: string[] = []
    const failure = new Error('plugin dispose failed')
    let origin: string | undefined

    await expect(fakeModule.withFakeSagServer(async server => {
      origin = server.origin
      await disposeAll([
        { async dispose() { calls.push('system') } },
        { async dispose() { calls.push('tools') } },
        { async dispose() { calls.push('plugin'); throw failure } },
      ])
    })).rejects.toBe(failure)

    expect(calls).toEqual(['plugin', 'tools', 'system'])
    if (origin === undefined) throw new Error('fake SAG was not started')
    await expect(fetch(`${origin}/api/v1/system/health`)).rejects.toThrow()
  })

  it('runs the local SAG main path through discovery, MCP handshake, REST upload, search, and read', async () => {
    const fakeModule = await import('../../../examples/local/fake-sag-server.mjs') as FakeSagModule
    await fakeModule.withFakeSagServer(async server => {
      const resources: Disposable[] = []
      try {
        const expected = JSON.parse(await readFile(resolve(root, 'examples/local/expected/upload-search-read.json'), 'utf8')) as unknown
        const uploadPath = '/workspace/uploaded.md'
        const bytes = new TextEncoder().encode('# Uploaded\n\nhello SAG')
        const ctx = new Context()
        const system = ctx.plugin(SystemPrompt)
        const tools = ctx.plugin(ToolRuntime)
        const settings = ctx.plugin(MemorySettingsProvider)
        resources.push(system, tools, settings)
        provideLocalServices(ctx, uploadPath, bytes)
        await Promise.all([system, tools, settings])
        const plugin = ctx.plugin(DshSag, { discoveryUrls: [server.origin] })
        resources.push(plugin)
        await plugin
        const sources = await ctx.tools.execute({
          callId: ToolCallId('local-sources'), name: 'sag_list_sources', signal: new AbortController().signal, arguments: {},
        })
        const status = await ctx.tools.execute({
          callId: ToolCallId('local-status'), name: 'sag_status', signal: new AbortController().signal, arguments: {},
        })
        const upload = await ctx.tools.execute({
          callId: ToolCallId('local-upload'), name: 'sag_upload_file', signal: new AbortController().signal,
          arguments: { path: uploadPath },
        })
        const uploadValue = value<{ readonly documentId: string }>(upload)
        const document = await ctx.tools.execute({
          callId: ToolCallId('local-document'), name: 'sag_get_document', signal: new AbortController().signal,
          arguments: { document_id: uploadValue.documentId },
        })
        const search = await ctx.tools.execute({
          callId: ToolCallId('local-search'), name: 'sag_search', signal: new AbortController().signal,
          arguments: { query: 'hello SAG' },
        })
        const searchValue = value<{ readonly evidences: readonly { readonly evidenceRef: string }[] }>(search)
        const evidenceRef = searchValue.evidences[0]?.evidenceRef
        if (evidenceRef === undefined) throw new Error('fake SAG returned no evidence')
        const read = await ctx.tools.execute({
          callId: ToolCallId('local-read'), name: 'sag_read', signal: new AbortController().signal,
          arguments: { evidence_ref: evidenceRef },
        })

        expect({
          tools: ctx.tools.schemas().map(schema => schema.name),
          sources: sources.value,
          status: status.value,
          upload: upload.value,
          document: document.value,
          search: search.value,
          read: read.value,
        }).toEqual(expected)
        expect(server.requests).toEqual(expect.arrayContaining([
          'GET /api/v1/system/dsh-connection',
          'GET /api/v1/system/health',
          'GET /api/v1/system/ready',
          'GET /api/v1/system/dsh',
          'GET /api/v1/sources',
          'MCP initialize',
          'MCP tools/list',
          'POST /api/v1/sources/source-1/documents',
          'UPLOAD field=file filename=uploaded.md contentType=text/markdown bytes=21',
          'GET /api/v1/sources/source-1/documents/document-1',
          'POST /api/v1/search',
          'GET /api/v1/sources/source-1/chunks/chunk-1',
        ]))
      } finally {
        await disposeAll(resources)
      }
    })
  })

  it('keeps status and search usable for a search-only SAG while unsupported tools fail locally', async () => {
    const fakeModule = await import('../../../examples/local/fake-sag-server.mjs') as FakeSagModule
    const server = await fakeModule.startFakeSagServer({
      capabilities: ['knowledge.search', 'future.unknown'],
      mcpTools: ['search'],
      searchWithoutUpload: true,
      disableSources: true,
    })
    const resources: Disposable[] = []
    try {
      const ctx = new Context()
      const system = ctx.plugin(SystemPrompt)
      const tools = ctx.plugin(ToolRuntime)
      const settings = ctx.plugin(MemorySettingsProvider)
      resources.push(system, tools, settings)
      provideLocalServices(ctx, '/unused.md', new Uint8Array())
      await Promise.all([system, tools, settings])
      const plugin = ctx.plugin(DshSag, { discoveryUrls: [server.origin] })
      resources.push(plugin)
      await plugin

      const status = await ctx.tools.execute({ callId: ToolCallId('reduced-status'), name: 'sag_status', signal: new AbortController().signal, arguments: {} })
      const search = await ctx.tools.execute({ callId: ToolCallId('reduced-search'), name: 'sag_search', signal: new AbortController().signal, arguments: { query: 'hello SAG' } })
      expect(status).toMatchObject({ isError: false, value: { capabilities: ['knowledge.search', 'future.unknown'] } })
      expect(search).toMatchObject({ isError: false, value: { evidences: [expect.objectContaining({ sourceId: 'source-1' })] } })
      expect(server.requests).not.toContain('GET /api/v1/sources')

      const before = [...server.requests]
      for (const [name, args] of [
        ['sag_list_sources', {}],
        ['sag_read', { evidence_ref: 'not-decoded' }],
        ['sag_get_document', { source_id: 'source-1', document_id: 'document-1' }],
      ] as const) {
        await expect(ctx.tools.execute({ callId: ToolCallId(`reduced-${name}`), name, signal: new AbortController().signal, arguments: args })).resolves.toMatchObject({ isError: true })
      }
      expect(server.requests).toEqual(before)
    } finally {
      await disposeAll(resources)
      await server.close()
    }
  })

  it('runs model-visible search and read through a real managed process', async () => {
    process.env.PYTHONPATH = fakeRoot
    const expected = JSON.parse(await readFile(resolve(root, 'examples/local/expected/search-read.json'), 'utf8'))
    const ctx = new Context()
    const system = ctx.plugin(SystemPrompt)
    const tools = ctx.plugin(ToolRuntime)
    const subprocess = ctx.plugin(LocalSubprocessRuntime)
    await Promise.all([system, tools, subprocess])
    const plugin = ctx.plugin(DshSag, {
      mode: 'embedded',
      pythonCommand: 'python3', envFile: '/unused/fake.env', cwd: fakeRoot,
      namespaces: [{ id: 'product-docs', label: '产品文档' }],
    })
    await plugin
    try {
      const search = await ctx.tools.execute({
        callId: ToolCallId('search-1'), name: 'sag_search', signal: new AbortController().signal,
        arguments: { query: 'DW-2412P30 上传限制' },
      })
      const evidenceRef = value<{ readonly evidences: readonly { readonly evidenceRef: string }[] }>(search).evidences[0]?.evidenceRef
      if (evidenceRef === undefined) throw new Error('fake embedded SAG returned no evidence')
      const read = await ctx.tools.execute({
        callId: ToolCallId('read-1'), name: 'sag_read', signal: new AbortController().signal,
        arguments: { evidence_ref: evidenceRef },
      })
      expect({
        tools: ctx.tools.schemas().map(schema => schema.name),
        search: search.value,
        read: read.value,
      }).toEqual(expected)
      expect((await ctx.systemPrompt.assemble()).sections.some(section => section.name === 'tool:dsh-sag')).toBe(true)

      const expectedCancel = JSON.parse(await readFile(resolve(root, 'examples/local/expected/cancel.json'), 'utf8'))
      const controller = new AbortController()
      const cancelled = ctx.tools.execute({
        callId: ToolCallId('search-cancel'), name: 'sag_search', signal: controller.signal,
        arguments: { query: 'slow cancellation probe' },
      })
      await new Promise(resolvePromise => setTimeout(resolvePromise, 20))
      controller.abort()
      expect(await cancelled).toMatchObject(expectedCancel)
    } finally {
      await plugin.dispose()
      await subprocess.dispose()
      await tools.dispose()
      await system.dispose()
    }
  })
})
