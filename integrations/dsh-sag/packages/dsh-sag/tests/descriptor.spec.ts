import { describe, expect, it } from 'vitest'
import { parseConnectionDescriptor } from '../src/connection/descriptor.ts'

describe('parseConnectionDescriptor', () => {
  it('accepts schema version 1 and rejects secrets or unknown versions', () => {
    expect(parseConnectionDescriptor({
      schemaVersion: 1,
      name: '我的 SAG',
      apiUrl: 'http://127.0.0.1:8000/api/v1/',
      mcpUrl: 'http://127.0.0.1:8000/mcp/',
      accessToken: 'sag_local_value',
      defaultSourceId: null,
    })).toMatchObject({
      schemaVersion: 1,
      name: '我的 SAG',
      apiUrl: 'http://127.0.0.1:8000/api/v1',
      mcpUrl: 'http://127.0.0.1:8000/mcp/',
    })
    expect(() => parseConnectionDescriptor({ schemaVersion: 2 })).toThrow(/schemaVersion 1/)
    expect(() => parseConnectionDescriptor({
      schemaVersion: 1,
      name: 'x',
      apiUrl: 'file:///tmp',
      mcpUrl: 'http://x/mcp/',
      accessToken: 'x',
    })).toThrow(/http/)
  })

  it.each([
    [{ schemaVersion: 1, name: '', apiUrl: 'https://sag.example/api', mcpUrl: 'https://sag.example/mcp/', accessToken: 'token' }, /name/],
    [{ schemaVersion: 1, name: 'SAG', apiUrl: 'https://sag.example/api', mcpUrl: 'https://sag.example/mcp/', accessToken: '' }, /accessToken/],
    [{ schemaVersion: 1, name: 'SAG', apiUrl: 'https://sag.example/api', mcpUrl: 'https://sag.example/mcp/', accessToken: 'token', defaultSourceId: 1 }, /defaultSourceId/],
    [{ schemaVersion: 1, name: 'SAG', apiUrl: 'https://sag.example/api', mcpUrl: 'https://sag.example/mcp/', accessToken: 'token', extra: true }, /unknown field/],
  ])('rejects invalid descriptor input %#', (value, expected) => {
    expect(() => parseConnectionDescriptor(value)).toThrow(expected)
  })

  it.each(['apiUrl', 'mcpUrl'] as const)('rejects credentials embedded in %s', field => {
    const secret = 'never-persist-this'
    const value = {
      schemaVersion: 1, name: 'SAG', apiUrl: 'https://sag.example/api',
      mcpUrl: 'https://sag.example/mcp/', accessToken: 'token',
      [field]: `https://user:${secret}@sag.example/path`,
    }
    expect(() => parseConnectionDescriptor(value)).toThrow(new RegExp(`${field}.*username or password`))
  })
})
