import { describe, expect, it, vi } from 'vitest'
import type { FileSystem, FsTarget } from '@deepseek-ai/dsh-fs'
import {
  discoverConnection,
  platformConnectionPaths,
  type DiscoveryFetch,
} from '../src/connection/discovery.ts'

const descriptor = {
  schemaVersion: 1,
  name: 'Local SAG',
  apiUrl: 'http://127.0.0.1:8000/api/v1',
  mcpUrl: 'http://127.0.0.1:8000/mcp/',
  accessToken: 'sag_local_token',
} as const

function aborted(signal: AbortSignal | undefined): void {
  if (signal?.aborted) throw signal.reason
}

function memoryFs(files: Readonly<Record<string, string>>): Pick<FileSystem, 'resolve' | 'readText'> {
  return {
    async resolve(path, options) {
      aborted(options?.signal)
      if (!(path in files)) throw new Error(`not found: ${path}`)
      return { targetKey: path as FsTarget['targetKey'], displayPath: path }
    },
    async readText(target, signal) {
      aborted(signal)
      const content = files[String(target.targetKey)]
      if (content === undefined) throw new Error(`not found: ${target.displayPath}`)
      return content
    },
  }
}

function response(value: unknown, status = 200): Awaited<ReturnType<DiscoveryFetch>> {
  return Promise.resolve({ ok: status >= 200 && status < 300, status, json: async () => value })
}

describe('platformConnectionPaths', () => {
  it('returns the SAG platform path and honours an explicit file override', () => {
    expect(platformConnectionPaths({}, 'darwin', '/Users/test')).toEqual([
      '/Users/test/Library/Application Support/SAG/dsh-connection.json',
    ])
    expect(platformConnectionPaths({ APPDATA: 'C:\\Users\\test\\AppData\\Roaming' }, 'win32', 'C:\\Users\\test')).toEqual([
      'C:\\Users\\test\\AppData\\Roaming\\SAG\\dsh-connection.json',
    ])
    expect(platformConnectionPaths({ XDG_CONFIG_HOME: '/tmp/config' }, 'linux', '/home/test')).toEqual([
      '/tmp/config/sag/dsh-connection.json',
    ])
    expect(platformConnectionPaths({ SAG_DSH_CONNECTION_FILE: '/tmp/export.json' }, 'linux', '/home/test')).toEqual([
      '/tmp/export.json',
    ])
  })
})

describe('discoverConnection', () => {
  it('returns the first valid file without probing configured loopback URLs', async () => {
    const fetch = vi.fn<DiscoveryFetch>()

    const result = await discoverConnection({
      fs: memoryFs({ '/fixtures/dsh-connection.json': JSON.stringify(descriptor) }),
      paths: ['/fixtures/missing.json', '/fixtures/dsh-connection.json'],
      urls: ['http://127.0.0.1:8000'],
      fetch,
    }, new AbortController().signal)

    expect(result.source).toBe('file')
    expect(result.descriptor).toEqual(descriptor)
    expect(fetch).not.toHaveBeenCalled()
  })

  it('records an invalid file then accepts the first valid loopback descriptor', async () => {
    const fetch = vi.fn<DiscoveryFetch>(async url => {
      expect(url).toBe('http://127.0.0.1:8000/api/v1/system/dsh-connection')
      return response(descriptor)
    })

    const result = await discoverConnection({
      fs: memoryFs({
        '/fixtures/invalid.json': JSON.stringify({ ...descriptor, unexpected: true }),
      }),
      paths: ['/fixtures/invalid.json'],
      urls: ['http://127.0.0.1:8000'],
      fetch,
    }, new AbortController().signal)

    expect(result.source).toBe('loopback')
    expect(result.descriptor).toEqual(descriptor)
    expect(result.diagnostics).toEqual(expect.arrayContaining([
      expect.objectContaining({ source: 'file', candidate: '/fixtures/invalid.json', message: expect.stringMatching(/unknown field/) }),
    ]))
  })

  it('reports every unusable candidate when neither a file nor loopback descriptor is valid', async () => {
    const result = await discoverConnection({
      fs: memoryFs({ '/fixtures/invalid.json': '{' }),
      paths: ['/fixtures/missing.json', '/fixtures/invalid.json'],
      urls: ['http://127.0.0.1:8000'],
      fetch: async () => response({ schemaVersion: 2 }),
    }, new AbortController().signal)

    expect(result).toMatchObject({ source: undefined, descriptor: undefined })
    expect(result.diagnostics).toEqual([
      expect.objectContaining({ source: 'file', candidate: '/fixtures/missing.json', message: expect.stringMatching(/not found/) }),
      expect.objectContaining({ source: 'file', candidate: '/fixtures/invalid.json', message: expect.stringMatching(/JSON/) }),
      expect.objectContaining({ source: 'loopback', candidate: 'http://127.0.0.1:8000/api/v1/system/dsh-connection', message: expect.stringMatching(/schemaVersion 1/) }),
    ])
    for (const diagnostic of result.diagnostics) {
      expect(diagnostic.action).toContain('dsh plugin --profile web exec dsh-sag setup')
      expect(diagnostic.action).not.toContain('run dsh-sag setup')
    }
  })

  it('keeps an invalid loopback candidate actionable instead of throwing from diagnostics', async () => {
    const fetch = vi.fn<DiscoveryFetch>()

    await expect(discoverConnection({
      fs: memoryFs({}),
      paths: [],
      urls: ['not a URL'],
      fetch,
    }, new AbortController().signal)).resolves.toMatchObject({
      source: undefined,
      diagnostics: [expect.objectContaining({ source: 'loopback', candidate: 'not a URL' })],
    })
    expect(fetch).not.toHaveBeenCalled()
  })

  it('rejects loopback userinfo before probing without exposing its token in diagnostics', async () => {
    const secret = 'sag_local_never_log_this'
    const fetch = vi.fn<DiscoveryFetch>()
    const result = await discoverConnection({
      fs: memoryFs({}),
      paths: [],
      urls: [`http://local-user:${secret}@127.0.0.1:8000`],
      fetch,
    }, new AbortController().signal)

    expect(fetch).not.toHaveBeenCalled()
    expect(result.diagnostics).toHaveLength(1)
    const diagnostic = result.diagnostics[0]!
    expect(diagnostic.message).toMatch(/username or password/)
    for (const field of [diagnostic.candidate, diagnostic.message, diagnostic.action]) {
      expect(field).not.toContain(secret)
    }
  })

  it.each(['file', 'loopback'] as const)('rejects descriptor URL userinfo from %s without exposing it', async source => {
    const secret = 'descriptor-password'
    const unsafe = { ...descriptor, apiUrl: `http://user:${secret}@127.0.0.1:8000/api/v1` }
    const result = await discoverConnection({
      fs: memoryFs(source === 'file' ? { '/fixtures/unsafe.json': JSON.stringify(unsafe) } : {}),
      paths: source === 'file' ? ['/fixtures/unsafe.json'] : [],
      urls: source === 'loopback' ? ['http://127.0.0.1:8000'] : [],
      fetch: async () => response(unsafe),
    }, new AbortController().signal)
    expect(result.descriptor).toBeUndefined()
    expect(JSON.stringify(result.diagnostics)).not.toContain(secret)
    expect(JSON.stringify(result.diagnostics)).toContain('username or password')
  })

  it('aborts one slow loopback probe after 1500ms and continues in configured order', async () => {
    vi.useFakeTimers()
    try {
      const fetch = vi.fn<DiscoveryFetch>((url, init) => {
        if (url === 'http://127.0.0.1:8000/api/v1/system/dsh-connection') {
          return new Promise((_resolve, reject) => {
            init.signal.addEventListener('abort', () => reject(init.signal.reason), { once: true })
          })
        }
        return response(descriptor)
      })

      const discovery = discoverConnection({
        fs: memoryFs({}),
        paths: [],
        urls: ['http://127.0.0.1:8000', 'http://localhost:8000'],
        fetch,
      }, new AbortController().signal)
      await vi.advanceTimersByTimeAsync(1500)

      await expect(discovery).resolves.toMatchObject({ source: 'loopback', descriptor })
      expect(fetch.mock.calls.map(([url]) => url)).toEqual([
        'http://127.0.0.1:8000/api/v1/system/dsh-connection',
        'http://localhost:8000/api/v1/system/dsh-connection',
      ])
    } finally {
      vi.useRealTimers()
    }
  })

  it('continues after 1500ms even when a fetch implementation ignores abort', async () => {
    vi.useFakeTimers()
    try {
      const discovery = discoverConnection({
        fs: memoryFs({}),
        paths: [],
        urls: ['http://127.0.0.1:8000', 'http://localhost:8000'],
        fetch: url => url.startsWith('http://127.0.0.1')
          ? new Promise(() => undefined)
          : response(descriptor),
      }, new AbortController().signal)
      await vi.advanceTimersByTimeAsync(1500)

      await expect(discovery).resolves.toMatchObject({ source: 'loopback', descriptor })
    } finally {
      vi.useRealTimers()
    }
  })
})
