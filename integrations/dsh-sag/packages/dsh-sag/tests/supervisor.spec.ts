import { PassThrough } from 'node:stream'
import { describe, expect, it } from 'vitest'
import type { SubprocessHandle, SubprocessSpawnSpec } from '@deepseek-ai/dsh-subprocess'
import { SagRuntimeSupervisor } from '../src/runtime/supervisor.ts'

function fakeRuntime(options: { health?: string; engineVersion?: string } = {}) {
  const stdin = new PassThrough()
  const stdout = new PassThrough()
  let doneResolve!: (value: { exitCode: number | null; signal: NodeJS.Signals | null }) => void
  const done = new Promise<{ exitCode: number | null; signal: NodeJS.Signals | null }>(resolve => { doneResolve = resolve })
  let terminateCalls = 0
  stdin.on('data', chunk => {
    for (const line of String(chunk).trim().split('\n')) {
      const request = JSON.parse(line)
      if (request.method === 'initialize') {
        stdout.write(`${JSON.stringify({
          jsonrpc: '2.0', id: request.id, result: {
            protocolVersion: '1.0', engineVersion: options.engineVersion ?? '0.14.0',
            health: options.health ?? 'available', evidenceRead: true, namespaces: ['product-docs'],
          },
        })}\n`)
      } else if (request.method === 'shutdown') {
        stdout.write(`${JSON.stringify({ jsonrpc: '2.0', id: request.id, result: {} })}\n`)
      }
    }
  })
  stdin.on('finish', () => doneResolve({ exitCode: 0, signal: null }))
  const handle: SubprocessHandle = {
    pid: 42, stdin, stdout, stderr: undefined, collected: {}, done,
    terminate() { terminateCalls += 1; doneResolve({ exitCode: null, signal: 'SIGTERM' }) },
    async waitForExit() { await done; return true },
  }
  let spawnSpec: SubprocessSpawnSpec | undefined
  const runtime = {
    async resolveExecutable(command: string) { expect(command).toBe('python3'); return '/runtime/python' },
    spawn(spec: SubprocessSpawnSpec) { spawnSpec = spec; return handle },
  }
  return { runtime, get spawnSpec() { return spawnSpec }, get terminateCalls() { return terminateCalls } }
}

const config = {
  pythonCommand: 'python3', envFile: '/config/sag.env', cwd: '/runtime',
  namespaces: [{ id: 'product-docs', label: '产品文档' }],
  maxReadEngines: 2, maxResults: 8, maxSnippetChars: 400, maxReadChars: 20_000,
  shutdownGraceMs: 1_000, allowDegraded: false,
}

describe('SagRuntimeSupervisor', () => {
  it('spawns through ctx.subprocess and becomes ready only after handshake', async () => {
    const fixture = fakeRuntime()
    const supervisor = await SagRuntimeSupervisor.start(fixture.runtime, config)

    expect(fixture.spawnSpec).toEqual({
      argv: [
        '/runtime/python', '-m', 'dsh_sag_runtime', '--env-file', '/config/sag.env',
        '--namespace', 'product-docs', '--max-read-engines', '2', '--max-results', '8',
        '--max-excerpt-chars', '400', '--max-read-chars', '20000',
      ],
      cwd: '/runtime',
      stdio: { stdin: 'pipe', stdout: 'pipe', stderr: { maxBytes: 65_536 } },
      graceMs: 1_000,
      env: { PYTHONUNBUFFERED: '1' },
    })
    expect(supervisor.client).toBeDefined()
    await supervisor.dispose()
    expect(fixture.terminateCalls).toBe(0)
  })

  it('terminates a sidecar whose engine version fails the handshake', async () => {
    const fixture = fakeRuntime({ engineVersion: '0.9.0' })
    await expect(SagRuntimeSupervisor.start(fixture.runtime, config)).rejects.toThrow(/0\.14\.0/)
    expect(fixture.terminateCalls).toBe(1)
  })

  it('admits degraded health only when explicitly configured', async () => {
    const blocked = fakeRuntime({ health: 'degraded' })
    await expect(SagRuntimeSupervisor.start(blocked.runtime, config)).rejects.toThrow(/degraded/)

    const admitted = fakeRuntime({ health: 'degraded' })
    const supervisor = await SagRuntimeSupervisor.start(admitted.runtime, { ...config, allowDegraded: true })
    await supervisor.dispose()
  })
})
