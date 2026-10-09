import { describe, expect, it } from 'vitest'
import { resolveConfig } from '../src/config.ts'
import { SAG_SYSTEM_PROMPT } from '../src/index.ts'
import { createReadTool } from '../src/tools/read.ts'
import { createSearchTool } from '../src/tools/search.ts'

class FakeClient {
  readonly calls: { method: string; params: Record<string, unknown>; signal?: AbortSignal }[] = []
  async request(method: 'search' | 'read', params: Record<string, unknown>, signal?: AbortSignal) {
    this.calls.push({ method, params, ...(signal ? { signal } : {}) })
    if (method === 'search') {
      return {
        query: '上传限制', evidences: [{
          evidenceRef: 'YWJj', namespaceId: 'product-docs', sourceId: 'manual',
          title: '上传说明', excerpt: '最大 100 MiB', score: 0.91,
        }],
      }
    }
    return { title: '上传说明', content: '甲😀乙', offset: 0, nextOffset: 3, totalChars: 8 }
  }
}

const config = resolveConfig({
  mode: 'embedded',
  pythonCommand: 'python3', envFile: '/config/sag.env',
  namespaces: [{ id: 'product-docs', label: '产品文档' }],
  maxResults: 8, maxReadChars: 20_000,
})

describe('model tools', () => {
  it('sag_search maps defaults, caps and cancellation into one canonical request', async () => {
    const client = new FakeClient()
    const tool = createSearchTool(client as any, config)
    const controller = new AbortController()

    const value = await tool.execute({ query: '上传限制', limit: 50 }, { signal: controller.signal } as any)

    expect(tool.name).toBe('sag_search')
    expect(Object.keys(tool.parameters.properties)).toEqual(['query', 'namespaces', 'mode', 'limit'])
    expect(tool.parameters.required).toEqual(['query'])
    expect(tool.isConcurrencySafe?.({ query: '上传限制' })).toBe(true)
    expect(client.calls[0]).toEqual({
      method: 'search',
      params: { query: '上传限制', namespaces: ['product-docs'], mode: 'fast', limit: 8 },
      signal: controller.signal,
    })
    expect(value).toMatchObject({ evidences: [{ evidenceRef: 'YWJj' }] })
    expect(tool.output.render({}, value as any)[0]).toMatchObject({ type: 'text' })
    expect((tool.output.render({}, value as any)[0] as any).text).toContain('1. [产品文档] 上传说明')
    expect(tool.presentCall?.({ query: '上传限制' })).toEqual({
      card: 'generic', title: 'Search SAG knowledge', kind: 'search', rawInput: '上传限制',
    })
  })

  it('sag_search rejects a namespace outside the configured allowlist', async () => {
    const tool = createSearchTool(new FakeClient() as any, config)
    await expect(tool.execute({ query: 'q', namespaces: ['private'] }, { signal: new AbortController().signal } as any))
      .rejects.toThrow(/configured/)
  })

  it('sag_read renders content and an explicit continuation call', async () => {
    const client = new FakeClient()
    const tool = createReadTool(client as any, config)
    const value = await tool.execute({ evidence_ref: 'YWJj' }, { signal: new AbortController().signal } as any)
    const text = (tool.output.render({}, value as any)[0] as any).text

    expect(tool.name).toBe('sag_read')
    expect(Object.keys(tool.parameters.properties)).toEqual(['evidence_ref', 'offset', 'max_chars', 'include_events'])
    expect(client.calls[0]?.params).toEqual({ evidenceRef: 'YWJj', offset: 0, maxChars: 20_000, includeEvents: false })
    expect(text).toContain('甲😀乙')
    expect(text).toContain('sag_read')
    expect(text).toContain('offset=3')
    expect(tool.presentCall?.({ evidence_ref: 'YWJj' })).toEqual({
      card: 'generic', title: 'Read SAG evidence', kind: 'read', rawInput: 'YWJj',
    })
  })

  it('publishes stable prompt guidance for search then read', () => {
    expect(SAG_SYSTEM_PROMPT).toContain('sag_search')
    expect(SAG_SYSTEM_PROMPT).toContain('sag_read')
  })
})
