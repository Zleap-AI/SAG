import { describe, expect, it, vi } from 'vitest'
import type { SagConnectionDescriptor } from '../src/connection/types.js'
import { decodeLocalEvidenceRef, encodeLocalEvidenceRef, SagApiError, SagApiClient } from '../src/local/api-client.ts'
import { createSagGateway } from '../src/local/gateway.ts'

const descriptor: SagConnectionDescriptor = {
  schemaVersion: 1,
  name: 'Local SAG',
  apiUrl: 'http://127.0.0.1:8000/api/v1',
  mcpUrl: 'http://127.0.0.1:8000/mcp/',
  accessToken: 'sag_local_value',
}

function json(value: unknown, status = 200): Response {
  return new Response(JSON.stringify(value), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

describe('SagApiClient', () => {
  it('maps source and structured search requests to the current SAG REST API', async () => {
    const fetch = vi.fn<typeof globalThis.fetch>(async (input) => {
      const url = String(input)
      if (url.endsWith('/sources')) return json([{ id: 'source-1', name: 'Notes' }])
      if (url.endsWith('/search')) {
        return json({
          query: '上传限制',
          sections: [{
            source_id: 'source-1', source_name: 'Notes', chunk_id: 'chunk-1',
            heading: 'Limits', content: '100 MB', score: 0.9, rank: 1,
          }],
          summary: '', stats: {},
        })
      }
      throw new Error(`unexpected URL ${url}`)
    })
    const client = new SagApiClient(descriptor, fetch)
    const signal = new AbortController().signal

    await expect(client.listSources(signal)).resolves.toEqual([{ id: 'source-1', name: 'Notes' }])
    const result = await client.search({ query: '上传限制', sourceIds: ['source-1'], topK: 8 }, signal)

    expect(fetch).toHaveBeenNthCalledWith(1, 'http://127.0.0.1:8000/api/v1/sources', expect.objectContaining({
      headers: expect.objectContaining({ Authorization: 'Bearer sag_local_value' }),
      signal,
    }))
    expect(fetch).toHaveBeenNthCalledWith(2, 'http://127.0.0.1:8000/api/v1/search', expect.objectContaining({
      body: JSON.stringify({ query: '上传限制', source_ids: ['source-1'], top_k: 8, save_exploration: false }),
      headers: expect.objectContaining({ Authorization: 'Bearer sag_local_value', 'Content-Type': 'application/json' }),
      method: 'POST',
      signal,
    }))
    expect(result.sections[0]).toMatchObject({ sourceId: 'source-1', chunkId: 'chunk-1' })
  })

  it('skips non-addressable nullable search sections while retaining addressable evidence', async () => {
    const client = new SagApiClient(descriptor, async () => json({
      query: 'q', summary: '', stats: {},
      sections: [
        { source_id: null, source_name: null, chunk_id: null, heading: 'summary', content: 'not addressable', score: 0.5, rank: 1 },
        { source_id: 'source-1', source_name: 'Notes', chunk_id: 'chunk-1', heading: 'note', content: 'addressable', score: 0.9, rank: 2 },
      ],
    }))
    await expect(client.search({ query: 'q' }, new AbortController().signal)).resolves.toMatchObject({
      sections: [{ sourceId: 'source-1', chunkId: 'chunk-1', content: 'addressable' }],
    })
  })

  it('reads capabilities with camel-case upload fields and does not expose settings endpoints', async () => {
    const fetch = vi.fn<typeof globalThis.fetch>(async input => {
      expect(String(input)).toBe('http://127.0.0.1:8000/api/v1/system/dsh')
      return json({
        schemaVersion: 1,
        capabilities: ['sources.list', 'documents.upload'],
        upload: { maxMb: 100, extensions: ['pdf', 'md'] },
        defaultSourceId: 'source-1',
      })
    })
    const client = new SagApiClient(descriptor, fetch)

    await expect(client.capabilities(new AbortController().signal)).resolves.toEqual({
      schemaVersion: 1,
      capabilities: ['sources.list', 'documents.upload'],
      upload: { maxMb: 100, extensions: ['pdf', 'md'] },
      defaultSourceId: 'source-1',
    })
    expect(client).not.toHaveProperty('exportConnection')
    expect(client).not.toHaveProperty('regenerateConnection')
    expect(client).not.toHaveProperty('updateSettings')
  })

  it('accepts a reduced capability descriptor without upload limits', async () => {
    const client = new SagApiClient(descriptor, async () => json({
      schemaVersion: 1,
      capabilities: ['sources.list', 'knowledge.search', 'future.unknown'],
    }))
    await expect(client.capabilities(new AbortController().signal)).resolves.toEqual({
      schemaVersion: 1,
      capabilities: ['sources.list', 'knowledge.search', 'future.unknown'],
    })
  })

  it('maps document operations, multipart bytes, and forwards every caller signal', async () => {
    const calls: Array<{ url: string; init: RequestInit }> = []
    const fetch = vi.fn<typeof globalThis.fetch>(async (input, init = {}) => {
      calls.push({ url: String(input), init })
      if (init.method === 'DELETE') return json({ detail: '文档已删除' })
      if (String(input).endsWith('/reprocess')) return json({
        id: 'job-1', type: 'reprocess_document', status: 'queued', source_id: null, document_id: null, progress: 0.25, attempts: 2, error: null,
      })
      if (String(input).endsWith('/ingest')) return json({ id: 'doc-text', status: 'pending' })
      if (init.method === 'POST') return json({ id: 'doc-file', status: 'pending' }, 201)
      if (String(input).endsWith('/documents')) return json([{ id: 'doc-1' }])
      return json({ id: 'doc-1', source_id: 'source-1', status: 'failed', progress: 50, chunk_count: 3, error: 'failed', error_layer: 'parser', error_stage: 'extract' })
    })
    const client = new SagApiClient(descriptor, fetch)
    const signal = new AbortController().signal

    await client.listDocuments('source-1', signal)
    await expect(client.getDocument('source-1', 'doc-1', signal)).resolves.toEqual({
      id: 'doc-1', source_id: 'source-1', sourceId: 'source-1', status: 'failed', progress: 50,
      chunk_count: 3, chunkCount: 3, error: 'failed', error_layer: 'parser', errorLayer: 'parser',
      error_stage: 'extract', errorStage: 'extract',
    })
    await client.uploadFile({ sourceId: 'source-1', filename: 'note.md', contentType: 'text/markdown', bytes: new Uint8Array([65, 66]) }, signal)
    await client.ingestText({ sourceId: 'source-1', title: 'Note', text: 'Body' }, signal)
    await expect(client.reprocessDocument('source-1', 'doc-1', signal)).resolves.toEqual({
      id: 'job-1', type: 'reprocess_document', status: 'queued', sourceId: null, documentId: null, progress: 0.25, attempts: 2, error: null,
    })
    await client.deleteDocument('source-1', 'doc-1', signal)

    expect(calls.map(call => call.url)).toEqual([
      'http://127.0.0.1:8000/api/v1/sources/source-1/documents',
      'http://127.0.0.1:8000/api/v1/sources/source-1/documents/doc-1',
      'http://127.0.0.1:8000/api/v1/sources/source-1/documents',
      'http://127.0.0.1:8000/api/v1/sources/source-1/documents/ingest',
      'http://127.0.0.1:8000/api/v1/sources/source-1/documents/doc-1/reprocess',
      'http://127.0.0.1:8000/api/v1/sources/source-1/documents/doc-1',
    ])
    expect(calls.every(call => call.init.signal === signal)).toBe(true)
    const upload = calls[2]!.init.body
    expect(upload).toBeInstanceOf(FormData)
    expect((upload as FormData).get('file')).toBeInstanceOf(Blob)
    expect(calls[2]!.init.headers).toEqual({ Authorization: 'Bearer sag_local_value' })
  })

  it('preserves the SAG error envelope without including the connector token', async () => {
    const fetch = vi.fn<typeof globalThis.fetch>(async () => json({
      error: {
        code: 'validation_error', message: `文件过大: ${descriptor.accessToken}`,
        layer: 'api', stage: 'upload', retryable: false, request_id: 'req-1',
      },
    }, 422))
    const client = new SagApiClient(descriptor, fetch)

    const error = await client.listSources(new AbortController().signal).catch((caught: unknown) => caught)
    expect(error).toBeInstanceOf(SagApiError)
    expect(error).toMatchObject({
      status: 422, code: 'validation_error', layer: 'api', stage: 'upload', retryable: false, requestId: 'req-1',
    })
    expect(String(error)).toContain('文件过大')
    expect(String(error)).not.toContain(descriptor.accessToken)
  })

  it('rejects a nullable job id and non-real job enum values', async () => {
    for (const payload of [
      { id: null, type: 'reprocess_document', status: 'queued', source_id: null, document_id: null, progress: 0, attempts: 0, error: null },
      { id: 'job-1', type: 'reprocess', status: 'pending', source_id: null, document_id: null, progress: 0, attempts: 0, error: null },
      { id: 'job-1', type: 'process_document', status: 'queued', source_id: null, document_id: null, progress: 0, attempts: 0, error: null },
    ]) {
      const client = new SagApiClient(descriptor, vi.fn<typeof globalThis.fetch>(async () => json(payload)))
      await expect(client.reprocessDocument('source-1', 'doc-1', new AbortController().signal)).rejects.toThrow(/job/)
    }
  })

  it('rejects non-integer document progress from the wire', async () => {
    const client = new SagApiClient(descriptor, vi.fn<typeof globalThis.fetch>(async () => json({
      id: 'doc-1', source_id: 'source-1', status: 'loading', progress: 0.5,
    })))
    await expect(client.getDocument('source-1', 'doc-1', new AbortController().signal)).rejects.toThrow(/integer percentage/)
  })

  it('requires the canonical chunk_id wire field and rejects a legacy id substitute', async () => {
    const client = new SagApiClient(descriptor, vi.fn<typeof globalThis.fetch>(async () => json({
      id: 'chunk-1', content: 'complete', source_id: 'source-1',
    })))

    await expect(client.readChunk('source-1', 'chunk-1', new AbortController().signal)).rejects.toThrow(/chunk.*chunk_id/i)
  })
})

describe('local evidence references and gateway', () => {
  it('uses one canonical base64url v1 representation and rejects malformed or extra data', () => {
    const encoded = encodeLocalEvidenceRef({ v: 1, sourceId: 'source-1', chunkId: 'chunk-1' })
    expect(encoded).toBe('eyJ2IjoxLCJzb3VyY2VJZCI6InNvdXJjZS0xIiwiY2h1bmtJZCI6ImNodW5rLTEifQ')
    expect(encoded).not.toMatch(/[+/=]/)
    expect(decodeLocalEvidenceRef(encoded)).toEqual({ v: 1, sourceId: 'source-1', chunkId: 'chunk-1' })
    expect(() => decodeLocalEvidenceRef('not-base64!')).toThrow(/evidence reference/i)
    expect(() => decodeLocalEvidenceRef('a'.repeat(4097))).toThrow(/too long/i)
    expect(() => decodeLocalEvidenceRef(Buffer.from(JSON.stringify({ v: 1, sourceId: 's', chunkId: 'c', extra: true })).toString('base64url'))).toThrow(/evidence reference/i)
    expect(() => decodeLocalEvidenceRef(Buffer.from(JSON.stringify({ v: 2, sourceId: 's', chunkId: 'c' })).toString('base64url'))).toThrow(/evidence reference/i)
  })

  it('turns structured search sections into evidence refs and reads only through source/chunk ids', async () => {
    const api = {
      search: vi.fn(async () => ({
        query: 'q', summary: '', stats: {},
        sections: [{ sourceId: 'source-1', sourceName: 'Notes', chunkId: 'chunk-1', heading: 'H', content: 'C', score: 1, rank: 1 }],
      })),
      readChunk: vi.fn(async () => ({ source_id: 'source-1', id: 'chunk-1', content: 'complete' })),
    }
    const gateway = createSagGateway(api as never)
    const signal = new AbortController().signal

    const result = await gateway.search({ query: 'q' }, signal)
    expect(result.evidences[0]).toMatchObject({ sourceId: 'source-1', chunkId: 'chunk-1' })
    await expect(gateway.read({ evidenceRef: result.evidences[0]!.evidenceRef }, signal)).resolves.toMatchObject({ content: 'complete' })
    expect(api.readChunk).toHaveBeenCalledWith('source-1', 'chunk-1', signal)
  })
})
