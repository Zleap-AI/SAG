import { describe, expect, it, vi } from 'vitest'
import type { FileSystem } from '@deepseek-ai/dsh-fs'
import { validateJsonSchemaValue, type JsonValue, type ToolRunContext } from '@deepseek-ai/dsh-tools'
import type { SagConnectionReport } from '../src/connection/manager.ts'
import type { SagConnectionDescriptor } from '../src/connection/types.ts'
import { resolveConfig } from '../src/config.ts'
import type { SagGateway } from '../src/local/gateway.ts'
import { encodeLocalEvidenceRef, SagApiError } from '../src/local/api-client.ts'
import { createDocumentTools, sagDeleteApprovalGate } from '../src/tools/documents.ts'
import { createIngestTextTool } from '../src/tools/ingest.ts'
import { createReadTool } from '../src/tools/read.ts'
import { createSearchTool } from '../src/tools/search.ts'
import { createSourceTools } from '../src/tools/sources.ts'
import { createStatusTool } from '../src/tools/status.ts'
import { createUploadTool } from '../src/tools/upload.ts'
import type { ConnectionManager } from '../src/tools/local.ts'

const descriptor: SagConnectionDescriptor = {
  schemaVersion: 1,
  name: 'My SAG',
  apiUrl: 'http://127.0.0.1:8000/api/v1',
  mcpUrl: 'http://127.0.0.1:8000/mcp/',
  accessToken: 'secret-token',
  defaultSourceId: 'default',
}

const config = resolveConfig({ mode: 'local', maxUploadBytes: 12 * 1024 * 1024, maxReadChars: 7 })
const exec = { signal: new AbortController().signal } as unknown as ToolRunContext
const localRef = encodeLocalEvidenceRef({ v: 1, sourceId: 'default', chunkId: 'chunk-1' })
const allCapabilities = [
  'sources.list', 'sources.create', 'knowledge.search', 'knowledge.read',
  'documents.list', 'documents.get', 'documents.upload', 'documents.ingest',
  'documents.reprocess', 'documents.delete',
] as const

function gateway() {
  return {
    health: vi.fn(),
    ready: vi.fn(),
    capabilities: vi.fn().mockResolvedValue({
      schemaVersion: 1,
      capabilities: [...allCapabilities],
      upload: { maxMb: 8, extensions: ['md', 'txt'] },
      defaultSourceId: 'default',
    }),
    listSources: vi.fn().mockResolvedValue([
      { id: 'default', name: 'Default' },
      { id: 'other', name: 'Other' },
    ]),
    createSource: vi.fn().mockResolvedValue({ id: 'new', name: 'New' }),
    search: vi.fn().mockResolvedValue({
      query: '上传限制', summary: '', stats: {}, evidences: [{
        evidenceRef: localRef, sourceId: 'default', sourceName: 'Default', chunkId: 'chunk-1',
        heading: '上传说明', content: '最大 100 MiB', score: 0.91, rank: 1,
      }],
    }),
    read: vi.fn().mockResolvedValue({ sourceId: 'default', chunkId: 'chunk-1', content: '甲😀乙丙丁' }),
    listDocuments: vi.fn().mockResolvedValue([{ id: 'doc-1', sourceId: 'default', status: 'ready' }]),
    getDocument: vi.fn().mockResolvedValue({ id: 'doc-1', sourceId: 'default', status: 'ready' }),
    uploadFile: vi.fn().mockResolvedValue({ id: 'doc-upload', sourceId: 'default', status: 'pending' }),
    ingestText: vi.fn().mockResolvedValue({ id: 'doc-text', sourceId: 'default', status: 'pending' }),
    reprocessDocument: vi.fn().mockResolvedValue({ id: 'job-1', sourceId: 'default', documentId: 'doc-1', type: 'reprocess_document', status: 'queued' }),
    deleteDocument: vi.fn().mockResolvedValue({ deleted: true }),
  } satisfies SagGateway
}

function managerWith(connectionGateway: SagGateway, overrides: Partial<SagConnectionReport> = {}): ConnectionManager {
  return {
    ensureConnected: vi.fn().mockResolvedValue({
      status: 'ready', health: true, ready: true, sourceCount: 2,
      descriptor, gateway: connectionGateway,
      capabilities: { schemaVersion: 1, capabilities: [...allCapabilities], upload: { maxMb: 8, extensions: ['md', 'txt'] }, defaultSourceId: 'fresh' },
      ...overrides,
    }),
  } as unknown as ConnectionManager
}

function fakeFs(bytes = new TextEncoder().encode('# note')) {
  return {
    resolve: vi.fn().mockResolvedValue({ targetKey: 'target', displayPath: '/tmp/a.md' }),
    stat: vi.fn().mockResolvedValue({ type: 'file', size: bytes.length, version: 'v1' }),
    readBytes: vi.fn().mockResolvedValue(bytes),
  } as unknown as FileSystem & {
    resolve: ReturnType<typeof vi.fn>
    stat: ReturnType<typeof vi.fn>
    readBytes: ReturnType<typeof vi.fn>
  }
}

describe('local SAG tools', () => {
  it('publishes eleven explicit tool names with read and write scheduling intent', () => {
    const api = gateway()
    const manager = managerWith(api)
    const fs = fakeFs()
    const tools = [
      createStatusTool(manager, config),
      ...createSourceTools(manager, config),
      createSearchTool(manager, config),
      createReadTool(manager, config),
      ...createDocumentTools(manager, config),
      createUploadTool(manager, config, fs),
      createIngestTextTool(manager, config),
    ]
    expect(tools.map(tool => tool.name)).toEqual([
      'sag_status', 'sag_list_sources', 'sag_create_source', 'sag_search', 'sag_read',
      'sag_list_documents', 'sag_get_document', 'sag_reprocess_document', 'sag_delete_document',
      'sag_upload_file', 'sag_ingest_text',
    ])
    const localSearch = tools.find(tool => tool.name === 'sag_search')!
    expect(Object.keys(localSearch.parameters.properties)).toEqual(['query', 'source_ids', 'strategy', 'limit'])
    expect(localSearch.parameters.properties.strategy).toMatchObject({ enum: ['vector', 'multi', 'multi_es_fast'] })
    expect(Object.keys((localSearch.output.schema as { properties: Record<string, unknown> }).properties)).toEqual(['query', 'evidences'])
    expect(JSON.stringify(localSearch.output.schema)).not.toContain('namespaceId')
    const localRead = tools.find(tool => tool.name === 'sag_read')!
    expect(Object.keys(localRead.parameters.properties)).toEqual(['evidence_ref', 'offset', 'max_chars'])
    for (const tool of tools) {
      expect(tool.description.length).toBeGreaterThan(20)
      expect(tool.output.schema).toMatchObject({ type: 'object' })
      expect(tool.presentCall).toBeTypeOf('function')
    }
    const schedulingArgs: Record<string, Record<string, unknown>> = {
      sag_search: { query: 'q' }, sag_read: { evidence_ref: 'ref' },
      sag_get_document: { document_id: 'doc' }, sag_reprocess_document: { document_id: 'doc' },
      sag_delete_document: { document_id: 'doc' }, sag_upload_file: { path: '/tmp/a.md' },
      sag_ingest_text: { text: 'text' }, sag_create_source: { name: 'source' },
    }
    expect(tools.filter(tool => tool.isConcurrencySafe?.(schedulingArgs[tool.name] ?? {} as never)).map(tool => tool.name)).toEqual([
      'sag_status', 'sag_list_sources', 'sag_search', 'sag_read', 'sag_list_documents', 'sag_get_document',
    ])
  })

  it('executes the positive contract of all eleven tools and keeps status transport-free', async () => {
    const api = gateway()
    api.getDocument.mockResolvedValue({
      id: 'doc-1', sourceId: 'fresh', filename: 'note.md', status: 'failed', progress: 50,
      chunkCount: 3, error: 'parse failed', errorLayer: 'parser', errorStage: 'extract',
    })
    api.reprocessDocument.mockResolvedValue({
      id: 'job-1', sourceId: null, documentId: null, type: 'reprocess_document', status: 'queued', progress: 0.25, attempts: 2, error: null,
    })
    const manager = managerWith(api)
    const [listSources, createSource] = createSourceTools(manager, config)
    const [listDocuments, getDocument, reprocess, remove] = createDocumentTools(manager, config)
    type PositiveTool = {
      name: string
      execute(args: unknown, context: ToolRunContext): Promise<unknown>
      output: { schema: Parameters<typeof validateJsonSchemaValue>[0]; render(args: unknown, value: JsonValue): Array<{ type: string; text?: string }> }
      presentCall?(args: unknown): unknown
    }
    const rows: Array<{
      tool: PositiveTool
      args: unknown
      title: string
      kind: string
      renderIncludes: string
      rawInput?: unknown
      locations?: Array<{ path: string }>
    }> = [
      { tool: createStatusTool(manager, config) as unknown as PositiveTool, args: {}, title: 'Check SAG status', kind: 'fetch', renderIncludes: 'sourceCount' },
      { tool: listSources as unknown as PositiveTool, args: {}, title: 'List SAG knowledge bases', kind: 'read', renderIncludes: 'Default' },
      { tool: createSource as unknown as PositiveTool, args: { name: 'New' }, title: 'Create SAG knowledge base', kind: 'edit', rawInput: 'New', renderIncludes: 'new' },
      { tool: createSearchTool(manager, config) as unknown as PositiveTool, args: { query: 'q' }, title: 'Search SAG knowledge', kind: 'search', rawInput: 'q', renderIncludes: 'evidence_ref' },
      { tool: createReadTool(manager, config) as unknown as PositiveTool, args: { evidence_ref: localRef }, title: 'Read SAG evidence', kind: 'read', rawInput: localRef, renderIncludes: '甲😀乙丙丁' },
      { tool: listDocuments as unknown as PositiveTool, args: {}, title: 'List SAG documents', kind: 'read', renderIncludes: 'doc-1' },
      { tool: getDocument as unknown as PositiveTool, args: { document_id: 'doc-1' }, title: 'Read SAG document status', kind: 'read', rawInput: 'doc-1', renderIncludes: 'parse failed' },
      { tool: reprocess as unknown as PositiveTool, args: { document_id: 'doc-1' }, title: 'Reprocess SAG document', kind: 'execute', rawInput: 'doc-1', renderIncludes: 'job-1' },
      { tool: remove as unknown as PositiveTool, args: { document_id: 'doc-1' }, title: 'Delete SAG document', kind: 'delete', rawInput: 'doc-1', renderIncludes: 'deleted' },
      { tool: createUploadTool(manager, config, fakeFs()) as unknown as PositiveTool, args: { path: '/tmp/a.md' }, title: 'Upload file to SAG', kind: 'edit', rawInput: '/tmp/a.md', locations: [{ path: '/tmp/a.md' }], renderIncludes: 'doc-upload' },
      { tool: createIngestTextTool(manager, config) as unknown as PositiveTool, args: { text: 'hello' }, title: 'Ingest text into SAG', kind: 'edit', rawInput: 'hello', renderIncludes: 'doc-text' },
    ]
    expect(rows.map(row => row.tool.name)).toEqual([
      'sag_status', 'sag_list_sources', 'sag_create_source', 'sag_search', 'sag_read', 'sag_list_documents',
      'sag_get_document', 'sag_reprocess_document', 'sag_delete_document', 'sag_upload_file', 'sag_ingest_text',
    ])
    for (const row of rows) {
      const value = await row.tool.execute(row.args, exec)
      expect(validateJsonSchemaValue(row.tool.output.schema, value), row.tool.name).toEqual([])
      const rendered = row.tool.output.render(row.args, value as JsonValue)
      expect(rendered, row.tool.name).toHaveLength(1)
      expect(rendered[0], row.tool.name).toMatchObject({ type: 'text' })
      expect(rendered[0]!.text, row.tool.name).toContain(row.renderIncludes)
      expect(row.tool.presentCall?.(row.args), row.tool.name).toEqual({
        card: 'generic', title: row.title, kind: row.kind,
        ...(row.rawInput === undefined ? {} : { rawInput: row.rawInput }),
        ...(row.locations === undefined ? {} : { locations: row.locations }),
      })
      if (row.tool.name === 'sag_status') expect(JSON.stringify(value)).not.toContain('apiUrl')
    }
  })

  it.each([
    ['pending', 0], ['loading', 35], ['ready', 100], ['failed', 60],
  ] as const)('projects real document status %s with integer progress', async (status, progress) => {
    const api = gateway()
    api.getDocument.mockResolvedValue({ id: 'doc-1', sourceId: 'fresh', status, progress })
    const get = createDocumentTools(managerWith(api), config)[1]
    await expect(get.execute({ document_id: 'doc-1' }, exec)).resolves.toMatchObject({ status, progress })
  })

  it.each(['queued', 'running', 'succeeded', 'failed'] as const)('projects real reprocess job status %s', async status => {
    const api = gateway()
    api.reprocessDocument.mockResolvedValue({
      id: `job-${status}`, type: 'reprocess_document', status, sourceId: null, documentId: null,
      progress: status === 'succeeded' ? 1 : 0.5, attempts: 1, error: status === 'failed' ? 'failed' : null,
    })
    const reprocess = createDocumentTools(managerWith(api), config)[2]
    await expect(reprocess.execute({ document_id: 'doc-1' }, exec)).resolves.toMatchObject({
      jobId: `job-${status}`, type: 'reprocess_document', status, sourceId: 'fresh', documentId: 'doc-1',
    })
  })

  it('declares integer document progress and strict real reprocess job fields', () => {
    const manager = managerWith(gateway())
    const [, get, reprocess] = createDocumentTools(manager, config)
    expect(validateJsonSchemaValue(get.output.schema, { id: 'doc', status: 'loading', progress: 0.5 })).not.toEqual([])
    expect(validateJsonSchemaValue(reprocess.output.schema, {
      accepted: true, jobId: null, documentId: 'doc', sourceId: 'source', type: 'reprocess', status: 'pending',
    })).not.toEqual([])
  })

  it('uses explicit source, then fresh capability default, descriptor fallback, then the only source', async () => {
    const api = gateway()
    const fs = fakeFs()
    const upload = createUploadTool(managerWith(api), config, fs)
    await upload.execute({ path: '/tmp/a.md', source_id: 'explicit' }, exec)
    expect(api.uploadFile).toHaveBeenLastCalledWith(expect.objectContaining({ sourceId: 'explicit' }), exec.signal)

    await upload.execute({ path: '/tmp/a.md' }, exec)
    expect(api.uploadFile).toHaveBeenLastCalledWith(expect.objectContaining({ sourceId: 'fresh' }), exec.signal)

    const descriptorFallback = createUploadTool(managerWith(api, {
      capabilities: { schemaVersion: 1, capabilities: ['documents.upload'], upload: { maxMb: 8, extensions: ['md'] } },
    }), config, fs)
    await descriptorFallback.execute({ path: '/tmp/a.md' }, exec)
    expect(api.uploadFile).toHaveBeenLastCalledWith(expect.objectContaining({ sourceId: 'default' }), exec.signal)

    const one = gateway()
    one.listSources.mockResolvedValue([{ id: 'only', name: 'Only' }])
    const noDefault = { ...descriptor, defaultSourceId: null }
    const oneUpload = createUploadTool(managerWith(one, {
      descriptor: noDefault,
      capabilities: { schemaVersion: 1, capabilities: ['documents.upload', 'sources.list'], upload: { maxMb: 8, extensions: ['md'] }, defaultSourceId: null },
    }), config, fs)
    await oneUpload.execute({ path: '/tmp/a.md' }, exec)
    expect(one.uploadFile).toHaveBeenLastCalledWith(expect.objectContaining({ sourceId: 'only' }), exec.signal)
  })

  it('returns actionable choices instead of silently selecting one of multiple sources', async () => {
    const api = gateway()
    const noDefault = { ...descriptor, defaultSourceId: null }
    const upload = createUploadTool(managerWith(api, {
      descriptor: noDefault,
      capabilities: { schemaVersion: 1, capabilities: ['documents.upload', 'sources.list'], upload: { maxMb: 8, extensions: ['md', 'txt'] }, defaultSourceId: null },
    }), config, fakeFs())
    await expect(upload.execute({ path: '/tmp/a.md' }, exec)).rejects.toThrow(/指定知识库.*default.*other/)
  })

  it('uploads only an admitted regular file through dsh fs and reports asynchronous acceptance', async () => {
    const api = gateway()
    const fs = fakeFs()
    const upload = createUploadTool(managerWith(api), config, fs)
    const result = await upload.execute({ path: '/tmp/a.md' }, exec)

    expect(fs.resolve).toHaveBeenCalledWith('/tmp/a.md', { signal: exec.signal })
    expect(fs.stat).toHaveBeenCalledWith(expect.anything(), exec.signal)
    expect(fs.readBytes).toHaveBeenCalledWith(expect.anything(), exec.signal, 8 * 1024 * 1024)
    expect(result).toEqual({ accepted: true, documentId: 'doc-upload', sourceId: 'fresh', status: 'pending' })
    expect(JSON.stringify(result)).not.toMatch(/complete|完成/)
  })

  it('rejects directories, over-limit files, and disallowed extensions before reading bytes', async () => {
    const api = gateway()
    const directoryFs = fakeFs()
    directoryFs.stat.mockResolvedValue({ type: 'directory', version: 'v1' })
    await expect(createUploadTool(managerWith(api), config, directoryFs).execute({ path: '/tmp/a.md' }, exec)).rejects.toThrow(/普通文件/)
    expect(directoryFs.readBytes).not.toHaveBeenCalled()

    const largeFs = fakeFs()
    largeFs.stat.mockResolvedValue({ type: 'file', size: 8 * 1024 * 1024 + 1, version: 'v1' })
    await expect(createUploadTool(managerWith(api), config, largeFs).execute({ path: '/tmp/a.md' }, exec)).rejects.toThrow(/8 MiB/)
    expect(largeFs.readBytes).not.toHaveBeenCalled()

    const badExtFs = fakeFs()
    badExtFs.resolve.mockResolvedValue({ targetKey: 'target', displayPath: '/tmp/a.exe' })
    await expect(createUploadTool(managerWith(api), config, badExtFs).execute({ path: '/tmp/a.exe' }, exec)).rejects.toThrow(/md.*txt/)
    expect(badExtFs.readBytes).not.toHaveBeenCalled()
  })

  it('uses both upload limits, admits exact and unknown sizes, enforces the final bound, and maps MIME', async () => {
    const api = gateway()
    const fourByteConfig = resolveConfig({ mode: 'local', maxUploadBytes: 4 })
    const exact = fakeFs(new Uint8Array(4))
    await createUploadTool(managerWith(api), fourByteConfig, exact).execute({ path: '/tmp/a.md' }, exec)
    expect(exact.readBytes).toHaveBeenCalledWith(expect.anything(), exec.signal, 4)

    const unknown = fakeFs(new Uint8Array(4))
    unknown.stat.mockResolvedValue({ type: 'file', version: 'v1' })
    await createUploadTool(managerWith(api), fourByteConfig, unknown).execute({ path: '/tmp/a.md' }, exec)
    expect(unknown.readBytes).toHaveBeenCalledWith(expect.anything(), exec.signal, 4)

    const brokenProvider = fakeFs(new Uint8Array(5))
    brokenProvider.stat.mockResolvedValue({ type: 'file', version: 'v1' })
    await expect(createUploadTool(managerWith(api), fourByteConfig, brokenProvider).execute({ path: '/tmp/a.md' }, exec)).rejects.toThrow(/上传上限/)
    expect(api.uploadFile).toHaveBeenCalledTimes(2)

    for (const [ext, expected] of [['md', 'text/markdown'], ['txt', 'text/plain'], ['json', 'application/json'], ['pdf', 'application/pdf'], ['bin', 'application/octet-stream']] as const) {
      const mimeApi = gateway()
      const fs = fakeFs()
      fs.resolve.mockResolvedValue({ targetKey: 'target', displayPath: `/tmp/a.${ext}` })
      const manager = managerWith(mimeApi, {
        capabilities: { schemaVersion: 1, capabilities: ['documents.upload'], upload: { maxMb: 8, extensions: [ext] }, defaultSourceId: 'fresh' },
      })
      await createUploadTool(manager, config, fs).execute({ path: `/tmp/a.${ext}` }, exec)
      expect(mimeApi.uploadFile).toHaveBeenCalledWith(expect.objectContaining({ contentType: expected }), exec.signal)
    }
  })

  it('ingests text and reports accepted without claiming background completion', async () => {
    const api = gateway()
    const ingest = createIngestTextTool(managerWith(api), config)
    await expect(ingest.execute({ text: 'hello', title: 'Note' }, exec)).resolves.toEqual({
      accepted: true, documentId: 'doc-text', sourceId: 'fresh', status: 'pending',
    })
  })

  it('maps local structured search to opaque refs and reads Unicode pages through REST', async () => {
    const api = gateway()
    const manager = managerWith(api)
    const search = createSearchTool(manager, config)
    const found = await search.execute({ query: '上传限制' }, exec)
    expect(api.search).toHaveBeenCalledWith({ query: '上传限制' }, exec.signal)
    expect(found).toMatchObject({ evidences: [{ evidenceRef: localRef, sourceId: 'default', title: '上传说明' }] })

    const read = createReadTool(manager, config)
    const page = await read.execute({ evidence_ref: localRef, offset: 1, max_chars: 2 }, exec)
    expect(api.read).toHaveBeenCalledWith({ evidenceRef: localRef }, exec.signal)
    expect(page).toEqual({ title: 'default/chunk-1', content: '😀乙', offset: 1, nextOffset: 3, totalChars: 5 })
    await expect(read.execute({ evidence_ref: localRef }, exec)).resolves.toMatchObject({ content: '甲😀乙丙丁' })
    await expect(read.execute({ evidence_ref: localRef, offset: 5 }, exec)).resolves.toEqual({
      title: 'default/chunk-1', content: '', offset: 5, totalChars: 5,
    })
    await expect(read.execute({ evidence_ref: 'x'.repeat(4097) }, exec)).rejects.toThrow(/too long/i)

    await expect(search.execute({ query: 'q', strategy: 'vector', source_ids: ['default'], limit: 50 }, exec)).resolves.toBeDefined()
    expect(api.search).toHaveBeenLastCalledWith({ query: 'q', sourceIds: ['default'], strategy: 'vector', topK: 50 }, exec.signal)
    await expect(search.execute({ query: 'q', limit: 51 }, exec)).rejects.toThrow(/50/)
  })

  it('caps local read pages by configuration and emits continuation only when content remains', async () => {
    const api = gateway()
    api.read.mockResolvedValue({ sourceId: 'default', chunkId: 'chunk-1', content: '甲😀乙丙丁戊己庚辛壬' })
    const read = createReadTool(managerWith(api), config)
    await expect(read.execute({ evidence_ref: localRef, max_chars: 100 }, exec)).resolves.toEqual({
      title: 'default/chunk-1', content: '甲😀乙丙丁戊己', offset: 0, nextOffset: 7, totalChars: 10,
    })
    await expect(read.execute({ evidence_ref: localRef, offset: 7, max_chars: 100 }, exec)).resolves.toEqual({
      title: 'default/chunk-1', content: '庚辛壬', offset: 7, totalChars: 10,
    })
  })

  it('uses the fixed actionable message and never leaks a connection token', async () => {
    const manager = { ensureConnected: vi.fn().mockResolvedValue({ status: 'not-found', health: false, ready: false, sourceCount: 0 }) } as unknown as ConnectionManager
    const tool = createStatusTool(manager, config)
    await expect(tool.execute({}, exec)).rejects.toThrow('没有发现正在运行的 SAG。请先启动 SAG 后重试；也可运行 dsh plugin --profile web exec dsh-sag setup 重新发现。')

    const unreachable = managerWith(gateway(), { status: 'unreachable', errors: [`fetch failed ${descriptor.accessToken}`] })
    await expect(createStatusTool(unreachable, config).execute({}, exec)).rejects.not.toThrow(descriptor.accessToken)
  })

  it('connects before validating every tool and gives all eleven the same not-found recovery', async () => {
    type Executable = { execute(args: unknown, context: ToolRunContext): Promise<unknown> }
    const missing = { ensureConnected: vi.fn().mockResolvedValue({ status: 'not-found', health: false, ready: false, sourceCount: 0 }) } as unknown as ConnectionManager
    const calls: Array<{ tool: Executable; args: unknown }> = [
      { tool: createStatusTool(missing, config) as unknown as Executable, args: {} },
      { tool: createSourceTools(missing, config)[0] as unknown as Executable, args: {} },
      { tool: createSourceTools(missing, config)[1] as unknown as Executable, args: { name: '' } },
      { tool: createSearchTool(missing, config) as unknown as Executable, args: { query: '' } },
      { tool: createReadTool(missing, config) as unknown as Executable, args: { evidence_ref: '' } },
      { tool: createDocumentTools(missing, config)[0] as unknown as Executable, args: {} },
      { tool: createDocumentTools(missing, config)[1] as unknown as Executable, args: { document_id: 'd' } },
      { tool: createDocumentTools(missing, config)[2] as unknown as Executable, args: { document_id: 'd' } },
      { tool: createDocumentTools(missing, config)[3] as unknown as Executable, args: { document_id: 'd' } },
      { tool: createUploadTool(missing, config, fakeFs()) as unknown as Executable, args: { path: '/tmp/a.md' } },
      { tool: createIngestTextTool(missing, config) as unknown as Executable, args: { text: '' } },
    ]
    for (const call of calls) await expect(call.tool.execute(call.args, exec)).rejects.toThrow('没有发现正在运行的 SAG')
    expect(missing.ensureConnected).toHaveBeenCalledTimes(11)
  })

  it('maps cached post-connect transport failures, preserves safe API errors, and preserves cancellation', async () => {
    const api = gateway()
    api.search.mockRejectedValue(new Error(`fetch JSON stack ${descriptor.accessToken}`))
    const search = createSearchTool(managerWith(api), config)
    const transport = await search.execute({ query: 'q' }, exec).catch((error: unknown) => error)
    expect(String(transport)).toContain('SAG 连接已中断')
    expect(String(transport)).not.toMatch(/fetch|JSON|stack|secret-token/)

    api.search.mockRejectedValue(new SagApiError(422, 'validation_error', '查询参数错误', false, 'req-1', 'api', 'search'))
    await expect(search.execute({ query: 'q' }, exec)).rejects.toMatchObject({ status: 422, code: 'validation_error', requestId: 'req-1' })

    api.search.mockRejectedValue(new SagApiError(422, 'validation_error', `查询参数错误 ${descriptor.accessToken}`, false, `req-${descriptor.accessToken}`, 'api', 'search'))
    const redactedError = await search.execute({ query: 'q' }, exec).catch((error: unknown) => error)
    expect(redactedError).toMatchObject({ status: 422, code: 'validation_error' })
    expect(String(redactedError)).not.toContain(descriptor.accessToken)

    const controller = new AbortController()
    const reason = new Error('caller cancelled')
    controller.abort(reason)
    await expect(search.execute({ query: 'q' }, { ...exec, signal: controller.signal })).rejects.toBe(reason)
  })

  it('maps a post-connect transport failure for every local tool without leaking its token', async () => {
    type Executable = { execute(args: unknown, context: ToolRunContext): Promise<unknown> }
    type Rejectable = { mockRejectedValue(value: unknown): unknown }
    const cases: Array<{ method: keyof SagGateway; build(manager: ConnectionManager, fs: FileSystem): Executable; args: unknown }> = [
      { method: 'listSources', build: manager => createSourceTools(manager, config)[0] as unknown as Executable, args: {} },
      { method: 'createSource', build: manager => createSourceTools(manager, config)[1] as unknown as Executable, args: { name: 'New' } },
      { method: 'search', build: manager => createSearchTool(manager, config) as unknown as Executable, args: { query: 'q' } },
      { method: 'read', build: manager => createReadTool(manager, config) as unknown as Executable, args: { evidence_ref: localRef } },
      { method: 'listDocuments', build: manager => createDocumentTools(manager, config)[0] as unknown as Executable, args: {} },
      { method: 'getDocument', build: manager => createDocumentTools(manager, config)[1] as unknown as Executable, args: { document_id: 'doc' } },
      { method: 'reprocessDocument', build: manager => createDocumentTools(manager, config)[2] as unknown as Executable, args: { document_id: 'doc' } },
      { method: 'deleteDocument', build: manager => createDocumentTools(manager, config)[3] as unknown as Executable, args: { document_id: 'doc' } },
      { method: 'uploadFile', build: (manager, fs) => createUploadTool(manager, config, fs) as unknown as Executable, args: { path: '/tmp/a.md' } },
      { method: 'ingestText', build: manager => createIngestTextTool(manager, config) as unknown as Executable, args: { text: 'text' } },
    ]
    for (const item of cases) {
      const api = gateway()
      ;(api[item.method] as unknown as Rejectable).mockRejectedValue(new Error(`fetch JSON stack ${descriptor.accessToken}`))
      const tool = item.build(managerWith(api), fakeFs())
      const error = await tool.execute(item.args, exec).catch((caught: unknown) => caught)
      expect(String(error)).toContain('SAG 连接已中断')
      expect(String(error)).not.toMatch(/fetch|JSON|stack|secret-token/)
    }
  })

  it('requires the delete operation to ask through tools/pre-execute', async () => {
    const next = vi.fn().mockResolvedValue({ kind: 'allow' })
    await expect(sagDeleteApprovalGate({ name: 'sag_delete_document' } as never, next)).resolves.toEqual({
      kind: 'ask', reason: '删除 SAG 文档后无法恢复',
    })
    expect(next).not.toHaveBeenCalled()
    await expect(sagDeleteApprovalGate({ name: 'sag_get_document' } as never, next)).resolves.toEqual({ kind: 'allow' })
  })

  it('fails locally before calling endpoints that SAG does not advertise', async () => {
    type Executable = { execute(args: unknown, context: ToolRunContext): Promise<unknown> }
    const api = gateway()
    const reduced = managerWith(api, {
      capabilities: { schemaVersion: 1, capabilities: ['knowledge.search'] },
    })
    const rows: Array<{ capability: string; method: keyof SagGateway; tool: Executable; args: unknown }> = [
      { capability: 'sources.list', method: 'listSources', tool: createSourceTools(reduced, config)[0] as unknown as Executable, args: {} },
      { capability: 'sources.create', method: 'createSource', tool: createSourceTools(reduced, config)[1] as unknown as Executable, args: { name: 'New' } },
      { capability: 'knowledge.read', method: 'read', tool: createReadTool(reduced, config) as unknown as Executable, args: { evidence_ref: localRef } },
      { capability: 'documents.list', method: 'listDocuments', tool: createDocumentTools(reduced, config)[0] as unknown as Executable, args: { source_id: 'default' } },
      { capability: 'documents.get', method: 'getDocument', tool: createDocumentTools(reduced, config)[1] as unknown as Executable, args: { source_id: 'default', document_id: 'doc' } },
      { capability: 'documents.reprocess', method: 'reprocessDocument', tool: createDocumentTools(reduced, config)[2] as unknown as Executable, args: { source_id: 'default', document_id: 'doc' } },
      { capability: 'documents.delete', method: 'deleteDocument', tool: createDocumentTools(reduced, config)[3] as unknown as Executable, args: { source_id: 'default', document_id: 'doc' } },
      { capability: 'documents.upload', method: 'uploadFile', tool: createUploadTool(reduced, config, fakeFs()) as unknown as Executable, args: { source_id: 'default', path: '/tmp/a.md' } },
      { capability: 'documents.ingest', method: 'ingestText', tool: createIngestTextTool(reduced, config) as unknown as Executable, args: { source_id: 'default', text: 'text' } },
    ]
    for (const row of rows) {
      await expect(row.tool.execute(row.args, exec), row.capability).rejects.toThrow(new RegExp(`does not provide ${row.capability}`))
      expect(api[row.method], row.capability).not.toHaveBeenCalled()
    }
    await expect(createSearchTool(reduced, config).execute({ query: 'q' }, exec)).resolves.toBeDefined()
  })

  it('uses explicit source ids for supported document operations without requiring sources.list', async () => {
    type Executable = { execute(args: unknown, context: ToolRunContext): Promise<unknown> }
    const api = gateway()
    const noDefault = { ...descriptor, defaultSourceId: null }
    const cases: Array<{ capability: string; method: keyof SagGateway; tool(manager: ConnectionManager): Executable; args: Record<string, unknown> }> = [
      { capability: 'documents.list', method: 'listDocuments', tool: manager => createDocumentTools(manager, config)[0] as unknown as Executable, args: {} },
      { capability: 'documents.get', method: 'getDocument', tool: manager => createDocumentTools(manager, config)[1] as unknown as Executable, args: { document_id: 'doc-1' } },
      { capability: 'documents.reprocess', method: 'reprocessDocument', tool: manager => createDocumentTools(manager, config)[2] as unknown as Executable, args: { document_id: 'doc-1' } },
      { capability: 'documents.delete', method: 'deleteDocument', tool: manager => createDocumentTools(manager, config)[3] as unknown as Executable, args: { document_id: 'doc-1' } },
      { capability: 'documents.upload', method: 'uploadFile', tool: manager => createUploadTool(manager, config, fakeFs()) as unknown as Executable, args: { path: '/tmp/a.md' } },
      { capability: 'documents.ingest', method: 'ingestText', tool: manager => createIngestTextTool(manager, config) as unknown as Executable, args: { text: 'note' } },
    ]
    for (const item of cases) {
      api.listSources.mockClear()
      ;(api[item.method] as ReturnType<typeof vi.fn>).mockClear()
      const manager = managerWith(api, {
        descriptor: noDefault,
        capabilities: {
          schemaVersion: 1, capabilities: [item.capability], defaultSourceId: null,
          ...(item.capability === 'documents.upload' ? { upload: { maxMb: 8, extensions: ['md'] } } : {}),
        },
      })
      const tool = item.tool(manager)
      await expect(tool.execute({ ...item.args, source_id: 'explicit-source' }, exec), item.capability).resolves.toBeDefined()
      expect(api.listSources, item.capability).not.toHaveBeenCalled()
      expect(api[item.method], item.capability).toHaveBeenCalled()

      ;(api[item.method] as ReturnType<typeof vi.fn>).mockClear()
      await expect(tool.execute(item.args, exec), item.capability).rejects.toThrow(/source_id/)
      expect(api.listSources, item.capability).not.toHaveBeenCalled()
      expect(api[item.method], item.capability).not.toHaveBeenCalled()
    }
  })
})
