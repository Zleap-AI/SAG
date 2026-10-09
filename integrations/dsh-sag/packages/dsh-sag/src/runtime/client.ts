import type { Readable, Writable } from 'node:stream'
import {
  SagProtocolError,
  decodeResponse,
  encodeRequest,
  type RpcRequest,
  type RpcResult,
} from './protocol.js'

interface PendingRequest {
  readonly resolve: (result: RpcResult) => void
  readonly reject: (error: Error) => void
  readonly removeAbort?: () => void
}

/** Structured rejection returned by the Python runtime. */
export class SagRuntimeError extends Error {
  override readonly name = 'SagRuntimeError'
  readonly code: string
  readonly data: Readonly<Record<string, unknown>>

  constructor(message: string, code: string, data: Readonly<Record<string, unknown>>) {
    super(message)
    this.code = code
    this.data = data
  }
}

/** Correlated JSON-RPC client over one managed sidecar's stdio streams. */
export class SagRuntimeClient {
  readonly #stdin: Writable
  readonly #stdout: Readable
  readonly #pending = new Map<number, PendingRequest>()
  readonly #decoder = new TextDecoder('utf-8', { fatal: true })
  #buffer = ''
  #nextId = 1
  #closedError: Error | undefined

  constructor(
    stdin: Writable,
    stdout: Readable,
    done: Promise<{ readonly exitCode: number | null; readonly signal: NodeJS.Signals | null }>,
  ) {
    this.#stdin = stdin
    this.#stdout = stdout
    stdout.on('data', (chunk: Buffer | string) => this.#consume(chunk))
    stdout.on('error', error => this.#failProtocol(error))
    stdin.on('error', error => this.#failProtocol(error))
    void done.then(
      outcome => this.#close(new Error(`SAG runtime exited (code=${outcome.exitCode ?? 'null'}, signal=${outcome.signal ?? 'null'})`)),
      error => this.#close(error instanceof Error ? error : new Error('SAG runtime spawn failed')),
    )
  }

  /** Send one request; cancellation remains live until the sidecar settles it. */
  request(method: 'initialize' | 'search' | 'read' | 'shutdown', params: Readonly<Record<string, unknown>>, signal?: AbortSignal): Promise<RpcResult> {
    if (this.#closedError) return Promise.reject(new Error(`SAG runtime client is closed: ${this.#closedError.message}`))
    if (this.#nextId > Number.MAX_SAFE_INTEGER) {
      this.#close(new Error('SAG runtime request id space exhausted'))
      return Promise.reject(this.#closedError)
    }
    const id = this.#nextId++
    return new Promise<RpcResult>((resolve, reject) => {
      let cancelled = false
      const onAbort = (): void => {
        if (cancelled || this.#closedError) return
        cancelled = true
        this.#stdin.write(encodeRequest({ jsonrpc: '2.0', method: '$/cancelRequest', params: { id } }))
      }
      const removeAbort = signal
        ? (): void => signal.removeEventListener('abort', onAbort)
        : undefined
      this.#pending.set(id, { resolve, reject, ...(removeAbort ? { removeAbort } : {}) })
      if (signal) {
        signal.addEventListener('abort', onAbort, { once: true })
      }
      const request = { jsonrpc: '2.0', id, method, params } as RpcRequest
      this.#stdin.write(encodeRequest(request))
      if (signal?.aborted) onAbort()
    })
  }

  /** Stop admission and close the protocol input. */
  closeInput(): void {
    this.#stdin.end()
  }

  #consume(chunk: Buffer | string): void {
    if (this.#closedError) return
    try {
      this.#buffer += typeof chunk === 'string'
        ? chunk
        : this.#decoder.decode(chunk, { stream: true })
      let newline = this.#buffer.indexOf('\n')
      while (newline >= 0) {
        const line = this.#buffer.slice(0, newline).replace(/\r$/, '')
        this.#buffer = this.#buffer.slice(newline + 1)
        if (line) this.#acceptLine(line)
        newline = this.#buffer.indexOf('\n')
      }
      if (Buffer.byteLength(this.#buffer, 'utf8') > 8 * 1024 * 1024) {
        throw new SagProtocolError('response frame exceeds 8 MiB')
      }
    } catch (error) {
      this.#failProtocol(error)
    }
  }

  #acceptLine(line: string): void {
    const response = decodeResponse(line)
    const pending = this.#pending.get(response.id)
    if (!pending) throw new SagProtocolError(`response id ${response.id} is not live`)
    this.#pending.delete(response.id)
    pending.removeAbort?.()
    if ('error' in response) {
      const data = response.error.data ?? { code: 'RUNTIME_ERROR' }
      pending.reject(new SagRuntimeError(response.error.message, data.code, data as unknown as Readonly<Record<string, unknown>>))
    } else {
      pending.resolve(response.result)
    }
  }

  #failProtocol(error: unknown): void {
    const message = error instanceof Error ? error.message : String(error)
    this.#close(new SagProtocolError(`SAG runtime protocol failure: ${message}`))
  }

  #close(error: Error): void {
    if (this.#closedError) return
    this.#closedError = error
    for (const pending of this.#pending.values()) {
      pending.removeAbort?.()
      pending.reject(error)
    }
    this.#pending.clear()
  }
}
