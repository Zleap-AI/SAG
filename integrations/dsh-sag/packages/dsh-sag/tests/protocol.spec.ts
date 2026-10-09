import { describe, expect, it } from 'vitest'
import {
  MAX_FRAME_BYTES,
  SagProtocolError,
  decodeResponse,
  encodeRequest,
} from '../src/runtime/protocol.ts'

describe('runtime protocol', () => {
  it('decodes a successful initialize response', () => {
    expect(decodeResponse(JSON.stringify({
      jsonrpc: '2.0',
      id: 1,
      result: {
        protocolVersion: '1.0',
        engineVersion: '0.14.0',
        health: 'available',
        evidenceRead: true,
        namespaces: ['product-docs'],
      },
    }))).toEqual({
      jsonrpc: '2.0',
      id: 1,
      result: {
        protocolVersion: '1.0',
        engineVersion: '0.14.0',
        health: 'available',
        evidenceRead: true,
        namespaces: ['product-docs'],
      },
    })
  })

  it('decodes a structured runtime error without accepting extra fields', () => {
    expect(decodeResponse(JSON.stringify({
      jsonrpc: '2.0',
      id: 7,
      error: {
        code: -32010,
        message: 'Search failed',
        data: { code: 'SEARCH_FAILED', retryable: false },
      },
    }))).toMatchObject({ id: 7, error: { data: { code: 'SEARCH_FAILED' } } })

    expect(() => decodeResponse(JSON.stringify({
      jsonrpc: '2.0', id: 7, result: {}, error: { code: -1, message: 'bad' },
    }))).toThrow(/exactly one/)
  })

  it('rejects malformed, oversized, and unsupported response frames safely', () => {
    const completePayload = 'secret-that-must-not-be-echoed'
    expect(() => decodeResponse('{bad json')).toThrow(SagProtocolError)
    expect(() => decodeResponse(JSON.stringify({ jsonrpc: '2.0', id: 2, result: { extra: completePayload } })))
      .toThrow(/response id 2/)
    try {
      decodeResponse(`${'x'.repeat(MAX_FRAME_BYTES)}xx`)
    } catch (error) {
      expect(error).toBeInstanceOf(SagProtocolError)
      expect(String(error)).not.toContain(completePayload)
    }
  })

  it('encodes requests as one bounded NDJSON frame', () => {
    expect(encodeRequest({
      jsonrpc: '2.0',
      id: 3,
      method: 'search',
      params: { query: '上传限制', namespaces: ['product-docs'], mode: 'fast', limit: 8 },
    })).toBe('{"jsonrpc":"2.0","id":3,"method":"search","params":{"query":"上传限制","namespaces":["product-docs"],"mode":"fast","limit":8}}\n')
  })
})
