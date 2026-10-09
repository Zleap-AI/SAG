import { PassThrough } from 'node:stream'
import { describe, expect, it } from 'vitest'
import { SagRuntimeClient } from '../src/runtime/client.ts'
import { MAX_FRAME_BYTES } from '../src/runtime/protocol.ts'

function fixture() {
  const stdin = new PassThrough()
  const stdout = new PassThrough()
  let exit!: (value: { exitCode: number | null; signal: NodeJS.Signals | null }) => void
  const done = new Promise<{ exitCode: number | null; signal: NodeJS.Signals | null }>(resolve => { exit = resolve })
  const client = new SagRuntimeClient(stdin, stdout, done)
  return { stdin, stdout, exit, client }
}

function requests(stream: PassThrough): Promise<string[]> {
  return new Promise(resolve => {
    let text = ''
    stream.on('data', chunk => { text += String(chunk) })
    setImmediate(() => resolve(text.trim().split('\n').filter(Boolean)))
  })
}

describe('SagRuntimeClient', () => {
  it('correlates split and out-of-order response frames', async () => {
    const { client, stdout } = fixture()
    const first = client.request('shutdown', {})
    const second = client.request('shutdown', {})
    stdout.write('{"jsonrpc":"2.0","id":2,"result":{}}\r\n{"jsonrpc":"2.0",')
    stdout.write('"id":1,"result":{}}\n')

    await expect(second).resolves.toEqual({})
    await expect(first).resolves.toEqual({})
  })

  it('sends cancellation once and waits for the sidecar response', async () => {
    const { client, stdin, stdout } = fixture()
    const controller = new AbortController()
    const pending = client.request('shutdown', {}, controller.signal)
    controller.abort()
    controller.abort()
    const lines = await requests(stdin)

    expect(lines.map(line => JSON.parse(line))).toEqual([
      { jsonrpc: '2.0', id: 1, method: 'shutdown', params: {} },
      { jsonrpc: '2.0', method: '$/cancelRequest', params: { id: 1 } },
    ])
    let settled = false
    pending.finally(() => { settled = true }).catch(() => {})
    await new Promise(resolve => setImmediate(resolve))
    expect(settled).toBe(false)
    stdout.write('{"jsonrpc":"2.0","id":1,"error":{"code":-32800,"message":"cancelled","data":{"code":"DSH_SAG_CANCELLED"}}}\n')
    await expect(pending).rejects.toMatchObject({ code: 'DSH_SAG_CANCELLED' })
  })

  it('rejects live requests when the process exits', async () => {
    const { client, exit } = fixture()
    const pending = client.request('shutdown', {})
    exit({ exitCode: 1, signal: null })
    await expect(pending).rejects.toThrow(/exited/)
  })

  it.each([
    ['malformed JSON', Buffer.from('{bad}\n')],
    ['invalid UTF-8', Buffer.from([0xff, 0x0a])],
    ['oversized frame', Buffer.from(`${'x'.repeat(MAX_FRAME_BYTES + 1)}\n`)],
    ['unknown response id', Buffer.from('{"jsonrpc":"2.0","id":99,"result":{}}\n')],
  ])('fails closed on %s', async (_label, frame) => {
    const { client, stdout } = fixture()
    const pending = client.request('shutdown', {})
    stdout.write(frame)
    await expect(pending).rejects.toThrow(/protocol/i)
    await expect(client.request('shutdown', {})).rejects.toThrow(/closed/i)
  })
})
