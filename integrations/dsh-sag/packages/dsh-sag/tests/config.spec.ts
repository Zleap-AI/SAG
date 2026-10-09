import { describe, expect, it } from 'vitest'
import { Config, resolveConfig } from '../src/config.ts'

const valid = {
  mode: 'embedded' as const,
  pythonCommand: 'python3',
  envFile: '/config/sag.env',
  namespaces: [{ id: 'product-docs', label: '产品文档' }],
}

describe('resolveConfig', () => {
  it('defaults to local discovery without sidecar settings', () => {
    expect(resolveConfig({})).toMatchObject({
      mode: 'local',
      discoveryUrls: ['http://127.0.0.1:8000', 'http://localhost:8000'],
      requestTimeoutMs: 30_000,
      maxUploadBytes: 100 * 1024 * 1024,
      maxReadChars: 40_000,
      connectionCacheTtlMs: 5_000,
    })
  })

  it('keeps local discovery defaults through the loader schema', () => {
    expect(resolveConfig(Config({}))).toMatchObject({
      mode: 'local',
      discoveryUrls: ['http://127.0.0.1:8000', 'http://localhost:8000'],
    })
    expect(resolveConfig(Config({
      discoveryUrls: ['http://127.0.0.1:9000'],
    }))).toMatchObject({
      mode: 'local',
      discoveryUrls: ['http://127.0.0.1:9000'],
    })
  })

  it.each([
    'http://user@127.0.0.1:8000',
    'http://user:password@127.0.0.1:8000',
  ])('rejects credential-bearing discovery URL %s at load', url => {
    expect(() => resolveConfig(Config({ discoveryUrls: [url] }))).toThrow(/username or password/)
  })

  it('validates the local read page bound', () => {
    expect(resolveConfig({ mode: 'local', maxReadChars: 7 })).toMatchObject({ maxReadChars: 7 })
    expect(() => resolveConfig({ mode: 'local', maxReadChars: 200_001 })).toThrow(/maxReadChars/)
  })

  it('makes the ready connection reuse window explicit and bounded', () => {
    expect(resolveConfig({ mode: 'local', connectionCacheTtlMs: 250 })).toMatchObject({ connectionCacheTtlMs: 250 })
    expect(() => resolveConfig({ mode: 'local', connectionCacheTtlMs: 0 })).toThrow(/connectionCacheTtlMs/)
  })

  it('requires sidecar settings only for explicit embedded mode', () => {
    expect(resolveConfig({
      mode: 'embedded',
      pythonCommand: 'python3',
      envFile: '/sag.env',
      namespaces: [{ id: 'docs', label: '文档' }],
    })).toMatchObject({ mode: 'embedded', pythonCommand: 'python3' })
    expect(() => resolveConfig({
      mode: 'embedded',
      envFile: '/sag.env',
      namespaces: [{ id: 'docs', label: '文档' }],
    } as any)).toThrow(/pythonCommand/)
  })

  it('resolves every deployment default once at load', () => {
    expect(resolveConfig(valid)).toMatchObject({
      pythonCommand: 'python3', envFile: '/config/sag.env', defaultMode: 'fast',
      maxResults: 20, maxSnippetChars: 1200, maxReadChars: 40_000,
      maxReadEngines: 4, requestTimeoutMs: 30_000, shutdownGraceMs: 5_000,
      allowDegraded: false,
    })
  })

  it.each([
    [{ ...valid, pythonCommand: '' }, /pythonCommand/],
    [{ ...valid, envFile: ' ' }, /envFile/],
    [{ ...valid, namespaces: [] }, /namespace/],
    [{ ...valid, namespaces: [{ id: 'x', label: 'X' }, { id: 'x', label: 'Y' }] }, /duplicate/],
    [{ ...valid, namespaces: [{ id: 'x'.repeat(37), label: 'X' }] }, /namespace id/],
    [{ ...valid, namespaces: [{ id: 'x', label: '' }] }, /label/],
    [{ ...valid, defaultMode: 'unknown' }, /defaultMode/],
    [{ ...valid, maxResults: 51 }, /maxResults/],
    [{ ...valid, maxReadChars: 200_001 }, /maxReadChars/],
    [{ ...valid, requestTimeoutMs: 0 }, /requestTimeoutMs/],
    [{ ...valid, shutdownGraceMs: 2_147_483_648 }, /shutdownGraceMs/],
  ] as const)('rejects invalid configuration %#', (raw, expected) => {
    expect(() => resolveConfig(raw as any)).toThrow(expected)
  })
})
