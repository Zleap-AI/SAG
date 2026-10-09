import { describe, expect, it, vi } from 'vitest'
import type { SagConnectionDescriptor } from '../src/connection/types.js'
import {
  inspectSagConnection,
  SagConnectionManager,
  type ConnectionInspection,
} from '../src/connection/manager.ts'
import { McpProbeIncompatibleError, McpProbeUnreachableError } from '../src/local/mcp-probe.ts'

const descriptor: SagConnectionDescriptor = {
  schemaVersion: 1, name: 'SAG', apiUrl: 'http://127.0.0.1:8000/api/v1',
  mcpUrl: 'http://127.0.0.1:8000/mcp/', accessToken: 'sag_local_manager_secret',
}
const healthy: ConnectionInspection = {
  status: 'ready', health: true, ready: true, capabilities: {
    schemaVersion: 1, capabilities: ['sources.list', 'knowledge.search'],
    upload: { maxMb: 100, extensions: ['pdf'] },
  }, sourceCount: 1, mcp: { tools: ['list_sources', 'read', 'search'], readTool: 'read' },
}

function manager(overrides: Partial<ConstructorParameters<typeof SagConnectionManager>[0]> = {}) {
  const store = { load: vi.fn(async () => undefined), save: vi.fn(async () => undefined) }
  const discover = vi.fn(async () => ({ source: 'loopback' as const, descriptor, diagnostics: [] }))
  const inspect = vi.fn(async () => healthy)
  return {
    instance: new SagConnectionManager({ store, discover, inspect, ...overrides }),
    store, discover, inspect,
  }
}

describe('SagConnectionManager', () => {
  it('loads and validates a saved connection without discovery', async () => {
    const setup = manager()
    setup.store.load.mockResolvedValue(descriptor)

    await expect(setup.instance.ensureConnected(new AbortController().signal)).resolves.toMatchObject({ descriptor, status: 'ready' })
    expect(setup.inspect).toHaveBeenCalledOnce()
    expect(setup.discover).not.toHaveBeenCalled()
    expect(setup.store.save).not.toHaveBeenCalled()
  })

  it('rediscovers after a saved connection fails without persisting from the runtime manager', async () => {
    const stale = { ...descriptor, apiUrl: 'http://127.0.0.1:7000/api/v1' }
    const setup = manager()
    setup.store.load.mockResolvedValue(stale)
    setup.inspect.mockResolvedValueOnce({ status: 'unreachable', health: false, ready: false, sourceCount: 0 })
      .mockResolvedValueOnce(healthy)

    await expect(setup.instance.ensureConnected(new AbortController().signal)).resolves.toMatchObject({ descriptor, status: 'ready' })
    expect(setup.discover).toHaveBeenCalledOnce()
    expect(setup.store.save).not.toHaveBeenCalled()
  })

  it('does not persist or report ready when discovery handshake is incompatible', async () => {
    const setup = manager()
    setup.inspect.mockResolvedValue({ status: 'incompatible', health: true, ready: true, sourceCount: 1 })

    await expect(setup.instance.ensureConnected(new AbortController().signal)).resolves.toMatchObject({ status: 'incompatible' })
    expect(setup.store.save).not.toHaveBeenCalled()
  })

  it('reports not-found without inspecting when discovery has no descriptor', async () => {
    const setup = manager()
    setup.discover.mockResolvedValue({ source: undefined, descriptor: undefined, diagnostics: [] })

    await expect(setup.instance.doctor(new AbortController().signal)).resolves.toEqual(expect.objectContaining({ status: 'not-found' }))
    expect(setup.inspect).not.toHaveBeenCalled()
  })

  it('doctor delegates only the non-mutating inspection and never calls search or upload', async () => {
    const search = vi.fn()
    const uploadFile = vi.fn()
    const setup = manager({ inspect: vi.fn(async () => healthy) })
    setup.store.load.mockResolvedValue(descriptor)
    ;(setup.instance as unknown as { search?: unknown }).search = search
    ;(setup.instance as unknown as { uploadFile?: unknown }).uploadFile = uploadFile

    await expect(setup.instance.doctor(new AbortController().signal)).resolves.toMatchObject({ status: 'ready', sourceCount: 1 })
    expect(search).not.toHaveBeenCalled()
    expect(uploadFile).not.toHaveBeenCalled()
  })

  it('doctor never persists a ready discovery when no connection is saved', async () => {
    const setup = manager()

    await expect(setup.instance.doctor(new AbortController().signal)).resolves.toMatchObject({ status: 'ready', descriptor })
    expect(setup.store.save).not.toHaveBeenCalled()
  })

  it('doctor never replaces a stale saved connection after successful rediscovery', async () => {
    const stale = { ...descriptor, apiUrl: 'http://127.0.0.1:7000/api/v1' }
    const setup = manager()
    setup.store.load.mockResolvedValue(stale)
    setup.inspect.mockResolvedValueOnce({ status: 'unreachable', health: false, ready: false, sourceCount: 0 })
      .mockResolvedValueOnce(healthy)

    await expect(setup.instance.doctor(new AbortController().signal)).resolves.toMatchObject({ status: 'ready', descriptor })
    expect(setup.store.save).not.toHaveBeenCalled()
  })

  it('single-flights concurrent discovery while caller cancellation stays isolated', async () => {
    const inspection: PromiseWithResolvers<ConnectionInspection> = Promise.withResolvers()
    const setup = manager({ inspect: vi.fn(() => inspection.promise) })
    const cancelled = new AbortController()
    const first = setup.instance.ensureConnected(cancelled.signal)
    const second = setup.instance.ensureConnected(new AbortController().signal)
    cancelled.abort(new Error('caller stopped waiting'))

    await expect(first).rejects.toThrow('caller stopped waiting')
    inspection.resolve(healthy)
    await expect(second).resolves.toMatchObject({ status: 'ready', descriptor })
    expect(setup.store.load).toHaveBeenCalledTimes(2)
    expect(setup.discover).toHaveBeenCalledOnce()
    expect(setup.store.save).not.toHaveBeenCalled()
  })

  it('cancels a caller during its own store read without affecting a later caller', async () => {
    const delayed = Promise.withResolvers<SagConnectionDescriptor | undefined>()
    const old = { ...descriptor, accessToken: 'cancelled-load-secret' }
    const store = {
      load: vi.fn().mockImplementationOnce(() => delayed.promise).mockResolvedValueOnce(descriptor),
      save: vi.fn(async () => undefined),
    }
    const inspect = vi.fn(async () => healthy)
    const setup = manager({ store, inspect })
    const cancelled = new AbortController()

    const first = setup.instance.ensureConnected(cancelled.signal)
    cancelled.abort(new Error('caller cancelled its config read'))
    await expect(first).rejects.toThrow('caller cancelled its config read')
    await expect(setup.instance.ensureConnected(new AbortController().signal)).resolves.toMatchObject({ descriptor })
    delayed.resolve(old)
    await vi.waitFor(() => expect(store.load).toHaveBeenCalledTimes(2))
    await Promise.resolve()

    expect(inspect).toHaveBeenCalledOnce()
    expect(inspect).toHaveBeenCalledWith(descriptor, expect.any(AbortSignal))
    expect(store.save).not.toHaveBeenCalled()
  })

  it('loads every same-descriptor caller while sharing one saved-descriptor inspection', async () => {
    const inspection = Promise.withResolvers<ConnectionInspection>()
    const store = { load: vi.fn(async () => descriptor), save: vi.fn(async () => undefined) }
    const inspect = vi.fn(() => inspection.promise)
    const setup = manager({
      store,
      inspect,
    })

    const first = setup.instance.ensureConnected(new AbortController().signal)
    const second = setup.instance.ensureConnected(new AbortController().signal)
    await vi.waitFor(() => expect(store.load).toHaveBeenCalledTimes(2))
    expect(inspect).toHaveBeenCalledOnce()
    inspection.resolve(healthy)

    await expect(Promise.all([first, second])).resolves.toEqual([
      expect.objectContaining({ descriptor }),
      expect.objectContaining({ descriptor }),
    ])
    expect(inspect).toHaveBeenCalledOnce()
    expect(setup.discover).not.toHaveBeenCalled()
  })

  it('loads a changed descriptor before flight admission and never joins the older keyed flight', async () => {
    const old = { ...descriptor, accessToken: 'old-secret-must-not-leak' }
    const fresh = {
      ...descriptor,
      apiUrl: 'http://127.0.0.1:9300/api/v1',
      mcpUrl: 'http://127.0.0.1:9300/mcp/',
      accessToken: 'fresh-secret-must-not-leak',
      defaultSourceId: 'fresh-default',
    }
    let saved = old
    const oldInspection = Promise.withResolvers<ConnectionInspection>()
    const oldGateway = { close: vi.fn(async () => undefined) }
    const freshGateway = { close: vi.fn(async () => undefined) }
    const store = { load: vi.fn(async () => saved), save: vi.fn(async () => undefined) }
    const inspect = vi.fn(candidate => candidate.accessToken === old.accessToken
      ? oldInspection.promise
      : Promise.resolve(healthy))
    const setup = manager({
      store,
      inspect,
      gateway: vi.fn(candidate => candidate.accessToken === old.accessToken ? oldGateway : freshGateway) as never,
    })

    const first = setup.instance.ensureConnected(new AbortController().signal)
    await vi.waitFor(() => expect(inspect).toHaveBeenCalledWith(old, expect.any(AbortSignal)))
    saved = fresh
    const second = await setup.instance.ensureConnected(new AbortController().signal)

    expect(store.load).toHaveBeenCalledTimes(2)
    expect(second).toMatchObject({ descriptor: fresh, gateway: freshGateway })
    oldInspection.resolve(healthy)
    const firstError = await first.catch((error: unknown) => error)
    expect(firstError).toBeInstanceOf(Error)
    expect(String(firstError)).not.toContain(old.accessToken)
    expect(String(firstError)).not.toContain(fresh.accessToken)

    const reused = await setup.instance.ensureConnected(new AbortController().signal)
    expect(reused).toMatchObject({ descriptor: fresh, gateway: freshGateway })
    expect(inspect).toHaveBeenCalledTimes(2)
    expect(oldGateway.close).toHaveBeenCalledOnce()
    expect(freshGateway.close).not.toHaveBeenCalled()
    expect(store.save).not.toHaveBeenCalled()
  })

  it('does not let an older delayed store read supersede a newer admitted descriptor', async () => {
    const old = { ...descriptor, accessToken: 'delayed-old-secret' }
    const fresh = { ...descriptor, accessToken: 'newer-fresh-secret', defaultSourceId: 'fresh-default' }
    const delayedOld = Promise.withResolvers<SagConnectionDescriptor | undefined>()
    const loaded: string[] = []
    const store = {
      load: vi.fn()
        .mockImplementationOnce(async () => {
          const captured = old
          loaded.push(captured.accessToken)
          return delayedOld.promise
        })
        .mockImplementationOnce(async () => {
          loaded.push(fresh.accessToken)
          return fresh
        }),
      save: vi.fn(async () => undefined),
    }
    const freshGateway = { close: vi.fn(async () => undefined) }
    const setup = manager({ store, gateway: vi.fn(() => freshGateway) as never })

    const first = setup.instance.ensureConnected(new AbortController().signal)
    const second = setup.instance.ensureConnected(new AbortController().signal)
    await expect(second).resolves.toMatchObject({ descriptor: fresh, gateway: freshGateway })
    delayedOld.resolve(old)
    const firstError = await first.catch((error: unknown) => error)

    expect(store.load).toHaveBeenCalledTimes(2)
    expect(loaded).toEqual([old.accessToken, fresh.accessToken])
    expect(firstError).toBeInstanceOf(Error)
    expect(String(firstError)).not.toContain(old.accessToken)
    expect(String(firstError)).not.toContain(fresh.accessToken)
    expect(setup.inspect).toHaveBeenCalledOnce()
    expect(freshGateway.close).not.toHaveBeenCalled()
    expect(store.save).not.toHaveBeenCalled()
  })

  it('does not join an automatic-discovery flight after setup saves a descriptor', async () => {
    const discovered = { ...descriptor, accessToken: 'automatic-old-secret' }
    const saved = { ...descriptor, accessToken: 'setup-fresh-secret', defaultSourceId: 'saved-default' }
    let stored: SagConnectionDescriptor | undefined
    const automaticInspection = Promise.withResolvers<ConnectionInspection>()
    const automaticGateway = { close: vi.fn(async () => undefined) }
    const savedGateway = { close: vi.fn(async () => undefined) }
    const store = { load: vi.fn(async () => stored), save: vi.fn(async () => undefined) }
    const discover = vi.fn(async () => ({ source: 'loopback' as const, descriptor: discovered, diagnostics: [] }))
    const inspect = vi.fn(candidate => candidate.accessToken === discovered.accessToken
      ? automaticInspection.promise
      : Promise.resolve(healthy))
    const setup = manager({
      store,
      discover,
      inspect,
      gateway: vi.fn(candidate => candidate.accessToken === discovered.accessToken ? automaticGateway : savedGateway) as never,
    })

    const automatic = setup.instance.ensureConnected(new AbortController().signal)
    await vi.waitFor(() => expect(inspect).toHaveBeenCalledWith(discovered, expect.any(AbortSignal)))
    stored = saved
    await expect(setup.instance.ensureConnected(new AbortController().signal)).resolves.toMatchObject({
      descriptor: saved,
      gateway: savedGateway,
    })
    automaticInspection.resolve(healthy)
    await expect(automatic).rejects.toThrow('superseded')

    expect(store.load).toHaveBeenCalledTimes(2)
    expect(discover).toHaveBeenCalledOnce()
    expect(automaticGateway.close).toHaveBeenCalledOnce()
    expect(savedGateway.close).not.toHaveBeenCalled()
    expect(store.save).not.toHaveBeenCalled()
  })

  it('reuses automatic discovery in memory within TTL, then rediscovers after expiry or restart', async () => {
    vi.useFakeTimers()
    try {
      const first = manager({ readyCacheTtlMs: 100 })
      await first.instance.ensureConnected(new AbortController().signal)
      await first.instance.ensureConnected(new AbortController().signal)
      expect(first.discover).toHaveBeenCalledOnce()
      expect(first.inspect).toHaveBeenCalledOnce()
      expect(first.store.save).not.toHaveBeenCalled()

      await vi.advanceTimersByTimeAsync(100)
      await first.instance.ensureConnected(new AbortController().signal)
      expect(first.discover).toHaveBeenCalledTimes(2)
      expect(first.inspect).toHaveBeenCalledTimes(2)
      expect(first.store.save).not.toHaveBeenCalled()

      const restarted = manager({ readyCacheTtlMs: 100 })
      await restarted.instance.ensureConnected(new AbortController().signal)
      expect(restarted.discover).toHaveBeenCalledOnce()
      expect(restarted.store.save).not.toHaveBeenCalled()
    } finally {
      vi.useRealTimers()
    }
  })

  it('reloads saved settings and credentials on every call but reuses a matching validated gateway', async () => {
    const rotated = { ...descriptor, apiUrl: 'http://127.0.0.1:9100/api/v1', mcpUrl: 'http://127.0.0.1:9100/mcp/', accessToken: 'rotated', defaultSourceId: 'new-default' }
    let saved = descriptor
    const firstGateway = { close: vi.fn(async () => undefined) }
    const secondGateway = { close: vi.fn(async () => undefined) }
    const gateway = vi.fn(candidate => candidate.accessToken === descriptor.accessToken ? firstGateway : secondGateway)
    const setup = manager({ store: { load: vi.fn(async () => saved), save: vi.fn(async () => undefined) }, gateway: gateway as never })
    const first = await setup.instance.ensureConnected(new AbortController().signal)
    const reused = await setup.instance.ensureConnected(new AbortController().signal)
    saved = rotated
    const replaced = await setup.instance.ensureConnected(new AbortController().signal)
    expect(reused.gateway).toBe(first.gateway)
    expect(replaced).toMatchObject({ descriptor: rotated })
    expect(replaced.gateway).not.toBe(first.gateway)
    expect(setup.inspect).toHaveBeenCalledTimes(2)
    expect(firstGateway.close).toHaveBeenCalledOnce()
    expect(secondGateway.close).not.toHaveBeenCalled()
  })

  it('refreshes an unchanged ready connection after its bounded cache lifetime', async () => {
    vi.useFakeTimers()
    try {
      const setup = manager({
        store: { load: vi.fn(async () => descriptor), save: vi.fn(async () => undefined) },
        readyCacheTtlMs: 100,
      })
      const first = await setup.instance.ensureConnected(new AbortController().signal)
      await setup.instance.ensureConnected(new AbortController().signal)
      expect(setup.inspect).toHaveBeenCalledOnce()
      await vi.advanceTimersByTimeAsync(100)
      const refreshed = await setup.instance.ensureConnected(new AbortController().signal)
      expect(setup.inspect).toHaveBeenCalledTimes(2)
      expect(refreshed.gateway).not.toBe(first.gateway)
    } finally {
      vi.useRealTimers()
    }
  })

  it('expires a half-open shared flight and lets a later call retry successfully', async () => {
    vi.useFakeTimers()
    try {
      const inspection = vi.fn().mockImplementationOnce(() => new Promise(() => undefined)).mockResolvedValueOnce(healthy)
      const setup = manager({ inspect: inspection, requestTimeoutMs: 50 })
      const first = setup.instance.ensureConnected(new AbortController().signal)
      const failed = expect(first).rejects.toThrow(/timed out/i)
      await vi.advanceTimersByTimeAsync(0)
      await vi.advanceTimersByTimeAsync(50)
      await failed
      await expect(setup.instance.ensureConnected(new AbortController().signal)).resolves.toMatchObject({ status: 'ready' })
      expect(inspection).toHaveBeenCalledTimes(2)
    } finally {
      vi.useRealTimers()
    }
  })

  it('does not wait for failed-inspection gateway cleanup that ignores abort, then retries', async () => {
    vi.useFakeTimers()
    try {
      let candidate = 0
      const gateway = vi.fn(() => {
        candidate += 1
        return { close: vi.fn(() => candidate === 1 ? new Promise<void>(() => undefined) : Promise.resolve()) }
      })
      const inspection = vi.fn()
        .mockRejectedValueOnce(new Error('handshake failed'))
        .mockResolvedValueOnce(healthy)
      const setup = manager({
        store: { load: vi.fn(async () => descriptor), save: vi.fn(async () => undefined) },
        inspect: inspection,
        gateway: gateway as never,
        requestTimeoutMs: 40,
      })
      const first = setup.instance.ensureConnected(new AbortController().signal)
      const failed = expect(first).rejects.toThrow('handshake failed')
      await vi.advanceTimersByTimeAsync(0)
      await failed
      await expect(setup.instance.ensureConnected(new AbortController().signal)).resolves.toMatchObject({ status: 'ready' })
      expect(gateway).toHaveBeenCalledTimes(2)
    } finally {
      vi.useRealTimers()
    }
  })

  it('applies the owned deadline to a store read that never settles', async () => {
    vi.useFakeTimers()
    try {
      const setup = manager({
        store: { load: vi.fn(() => new Promise(() => undefined)), save: vi.fn(async () => undefined) },
        requestTimeoutMs: 25,
      })
      const running = setup.instance.ensureConnected(new AbortController().signal)
      const failed = expect(running).rejects.toThrow(/timed out/i)
      await vi.advanceTimersByTimeAsync(25)
      await failed
    } finally {
      vi.useRealTimers()
    }
  })

  it('returns a changed ready gateway without waiting for obsolete gateway close', async () => {
    vi.useFakeTimers()
    try {
      const rotated = { ...descriptor, accessToken: 'rotated' }
      let saved = descriptor
      const firstGateway = { close: vi.fn(() => new Promise<void>(() => undefined)) }
      const secondGateway = { close: vi.fn(async () => undefined) }
      const setup = manager({
        store: { load: vi.fn(async () => saved), save: vi.fn(async () => undefined) },
        gateway: vi.fn(candidate => candidate.accessToken === descriptor.accessToken ? firstGateway : secondGateway) as never,
        requestTimeoutMs: 25,
      })
      await setup.instance.ensureConnected(new AbortController().signal)
      saved = rotated
      const replacement = setup.instance.ensureConnected(new AbortController().signal)
      await vi.advanceTimersByTimeAsync(0)
      await expect(replacement).resolves.toMatchObject({ descriptor: rotated, gateway: secondGateway })
      expect(firstGateway.close).toHaveBeenCalledOnce()
    } finally {
      vi.useRealTimers()
    }
  })

  it('fences a timed-out discovery that completes after a newer descriptor becomes ready', async () => {
    vi.useFakeTimers()
    try {
      const stale = { ...descriptor, accessToken: 'stale-token' }
      const current = { ...descriptor, apiUrl: 'http://127.0.0.1:9200/api/v1', accessToken: 'current-token' }
      const staleInspection = Promise.withResolvers<ConnectionInspection>()
      const store = {
        load: vi.fn().mockResolvedValue(current).mockResolvedValueOnce(undefined),
        save: vi.fn(async () => undefined),
      }
      const staleGateway = { close: vi.fn(async () => undefined) }
      const currentGateway = { close: vi.fn(async () => undefined) }
      const inspect = vi.fn(candidate => candidate.accessToken === stale.accessToken ? staleInspection.promise : Promise.resolve(healthy))
      const setup = manager({
        store,
        discover: vi.fn(async () => ({ source: 'loopback', descriptor: stale, diagnostics: [] })),
        inspect,
        gateway: vi.fn(candidate => candidate.accessToken === stale.accessToken ? staleGateway : currentGateway) as never,
        requestTimeoutMs: 20,
      })
      const first = setup.instance.ensureConnected(new AbortController().signal)
      const timedOut = expect(first).rejects.toThrow(/timed out/i)
      await vi.advanceTimersByTimeAsync(20)
      await timedOut

      await expect(setup.instance.ensureConnected(new AbortController().signal)).resolves.toMatchObject({ descriptor: current, gateway: currentGateway })
      staleInspection.resolve(healthy)
      await vi.advanceTimersByTimeAsync(0)
      await expect(setup.instance.ensureConnected(new AbortController().signal)).resolves.toMatchObject({ descriptor: current, gateway: currentGateway })
      expect(store.save).not.toHaveBeenCalled()
      expect(staleGateway.close).toHaveBeenCalledOnce()
      expect(currentGateway.close).not.toHaveBeenCalled()
      expect(inspect).toHaveBeenCalledTimes(2)
    } finally {
      vi.useRealTimers()
    }
  })

  it.each(['ready', 'not-found', 'unreachable', 'incompatible'] as const)('uses the documented %s status', async status => {
    const setup = manager()
    if (status === 'not-found') setup.discover.mockResolvedValue({ source: undefined, descriptor: undefined, diagnostics: [] })
    else setup.inspect.mockResolvedValue({ ...healthy, status })
    const report = await setup.instance.doctor(new AbortController().signal)
    expect(report.status).toBe(status)
  })
})

describe('inspectSagConnection status classification', () => {
  function gateway() {
    return {
      health: vi.fn(async () => ({ status: 'ok' })),
      ready: vi.fn(async () => ({ status: 'ready' })),
      capabilities: vi.fn(async () => healthy.capabilities!),
      listSources: vi.fn(async () => [{ id: 'source-1', name: 'Notes' }]),
    }
  }

  it('classifies a reachable MCP with missing tools as incompatible', async () => {
    await expect(inspectSagConnection(descriptor, new AbortController().signal, gateway() as never, async () => {
      throw new McpProbeIncompatibleError('missing search')
    })).resolves.toMatchObject({ status: 'incompatible', health: true, ready: true })
  })

  it.each(['ready', 'api', 'mcp'] as const)('classifies %s connection failure as unreachable', async failure => {
    const api = gateway()
    if (failure === 'ready') api.ready.mockRejectedValue(new Error('not ready'))
    if (failure === 'api') api.listSources.mockRejectedValue(new Error('ECONNREFUSED'))
    const mcp = async () => {
      if (failure === 'mcp') throw new McpProbeUnreachableError('ECONNREFUSED')
      return healthy.mcp!
    }
    await expect(inspectSagConnection(descriptor, new AbortController().signal, api as never, mcp)).resolves.toMatchObject({ status: 'unreachable' })
  })

  it('requires only MCP operations advertised by a reduced SAG capability set', async () => {
    const api = gateway()
    api.capabilities.mockResolvedValue({ schemaVersion: 1, capabilities: ['knowledge.search'] })
    const mcp = vi.fn(async (_descriptor, _signal, capabilities: readonly string[]) => {
      expect(capabilities).toEqual(['knowledge.search'])
      return { tools: ['search'] }
    })
    await expect(inspectSagConnection(descriptor, new AbortController().signal, api as never, mcp)).resolves.toMatchObject({
      status: 'ready', capabilities: { capabilities: ['knowledge.search'] }, mcp: { tools: ['search'] },
    })
  })

  it('does not call the source-list endpoint when sources.list is not advertised', async () => {
    const api = gateway()
    api.capabilities.mockResolvedValue({ schemaVersion: 1, capabilities: ['knowledge.search'] })
    api.listSources.mockRejectedValue(new Error('404 sources disabled'))
    await expect(inspectSagConnection(descriptor, new AbortController().signal, api as never, async () => ({ tools: ['search'] }))).resolves.toMatchObject({
      status: 'ready', sourceCount: 0,
    })
    expect(api.listSources).not.toHaveBeenCalled()
  })
})
