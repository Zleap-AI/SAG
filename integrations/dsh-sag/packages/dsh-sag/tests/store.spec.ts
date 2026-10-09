import { describe, expect, it } from 'vitest'
import { credentialKey, type CredentialKey, type CredentialRecord } from '@deepseek-ai/dsh-credentials'
import type { SagConnectionDescriptor } from '../src/connection/types.js'
import type { SagLocalSettings } from '../src/connection/types.js'
import {
  SAG_SETTINGS_NAMESPACE, SagConnectionStore, type SagConnectionStoreDeps, type SagConnectionSettings,
} from '../src/connection/store.js'

const descriptor: SagConnectionDescriptor = {
  schemaVersion: 1,
  name: '我的 SAG',
  apiUrl: 'https://sag.example.test/api',
  mcpUrl: 'https://sag.example.test/mcp',
  accessToken: 'secret-token',
  defaultSourceId: 'source-1',
}

const DEFAULT_SETTINGS: SagLocalSettings = {
  schemaVersion: 1,
  mode: 'local',
  name: '',
  apiUrl: '',
  mcpUrl: '',
  defaultSourceId: null,
  credentialId: 'local',
}

function memoryStore(): {
  store: SagConnectionStore
  settings: { get(): SagLocalSettings; replace(next: object): Promise<void> }
  credentials: {
    replace(key: CredentialKey, record: unknown): void
    readRecord(key: CredentialKey): Promise<CredentialRecord | undefined>
  }
} {
  let settings: Record<string, unknown> = {}
  const records = new Map<CredentialKey, unknown>()
  const credentials = {
    async readRecord(key: CredentialKey): Promise<CredentialRecord | undefined> {
      return records.get(key) as CredentialRecord | undefined
    },
    async modifyRecord(
      key: CredentialKey,
      mutate: (current: CredentialRecord | undefined) => Promise<CredentialRecord | undefined>,
    ): Promise<CredentialRecord | undefined> {
      const next = await mutate(records.get(key) as CredentialRecord | undefined)
      if (next !== undefined) records.set(key, next)
      return next
    },
    async deleteRecord(key: CredentialKey): Promise<void> {
      records.delete(key)
    },
    replace(key: CredentialKey, record: unknown): void {
      records.set(key, record)
    },
  }
  const scope = {
    get(): SagLocalSettings {
      return { ...DEFAULT_SETTINGS, ...settings } as SagLocalSettings
    },
    async update(patch: object): Promise<void> {
      settings = { ...settings, ...patch }
    },
    async replace(next: object): Promise<void> {
      settings = { ...next }
    },
  } satisfies SagConnectionSettings
  const deps = { credentials, settings: scope } satisfies SagConnectionStoreDeps
  return { store: new SagConnectionStore(deps), settings: scope, credentials }
}

describe('SagConnectionStore', () => {
  it('stores endpoints in settings and the token in one owned grant record', async () => {
    const { store, settings, credentials } = memoryStore()

    await store.save(descriptor)

    expect(settings.get()).toMatchObject({
      mode: 'local',
      name: '我的 SAG',
      apiUrl: descriptor.apiUrl,
      mcpUrl: descriptor.mcpUrl,
      credentialId: 'local',
    })
    expect(settings.get()).not.toHaveProperty('accessToken')
    await expect(credentials.readRecord(credentialKey('dsh-sag', 'local'))).resolves.toEqual({
      kind: 'grant',
      payload: { schemaVersion: 1, accessToken: descriptor.accessToken },
    })
    await expect(store.load()).resolves.toEqual(descriptor)
  })

  it('reads the current stored grant on every load', async () => {
    const { store, credentials } = memoryStore()
    await store.save(descriptor)
    credentials.replace(credentialKey('dsh-sag', 'local'), {
      kind: 'grant', payload: { schemaVersion: 1, accessToken: 'rotated-token' },
    })

    await expect(store.load()).resolves.toEqual({ ...descriptor, accessToken: 'rotated-token' })
  })

  it('rejects a malformed stored grant with a structured configuration error', async () => {
    const { store, credentials } = memoryStore()
    await store.save(descriptor)
    credentials.replace(credentialKey('dsh-sag', 'local'), {
      kind: 'grant', payload: { schemaVersion: 1, accessToken: '' },
    })

    await expect(store.load()).rejects.toMatchObject({
      code: 'SAG_CONNECTION_CONFIG_INVALID',
      field: 'credential.payload.accessToken',
    })
  })

  it.each([
    ['name', '', 'settings.name'],
    ['apiUrl', 'not a URL', 'settings.apiUrl'],
    ['mcpUrl', 'ftp://sag.example.test/mcp', 'settings.mcpUrl'],
    ['defaultSourceId', '', 'settings.defaultSourceId'],
  ] as const)('reports the malformed stored %s field', async (setting, value, field) => {
    const { store, settings } = memoryStore()
    await store.save(descriptor)
    await settings.replace({ ...settings.get(), [setting]: value })

    await expect(store.load()).rejects.toMatchObject({
      code: 'SAG_CONNECTION_CONFIG_INVALID',
      field,
    })
  })

  it.each(['apiUrl', 'mcpUrl'] as const)('never accepts userinfo from stored %s settings', async field => {
    const { store, settings } = memoryStore()
    await store.save(descriptor)
    await settings.replace({ ...settings.get(), [field]: 'https://user:secret@sag.example/path' })
    await expect(store.load()).rejects.toMatchObject({ field: `settings.${field}` })
  })

  it('clears only the owned grant and resets settings to their registered defaults', async () => {
    const { store, settings, credentials } = memoryStore()
    const otherKey = credentialKey('another-plugin', 'other')
    const otherRecord: CredentialRecord = { kind: 'grant', payload: { accessToken: 'other-token' } }
    credentials.replace(otherKey, otherRecord)
    await store.save(descriptor)

    await store.clear()

    expect(settings.get()).toEqual(DEFAULT_SETTINGS)
    await expect(credentials.readRecord(credentialKey('dsh-sag', 'local'))).resolves.toBeUndefined()
    await expect(credentials.readRecord(otherKey)).resolves.toEqual(otherRecord)
    await expect(store.load()).resolves.toBeUndefined()
  })
})

describe('registerSagSettings', () => {
  it('uses a host-compatible literal settings namespace', () => {
    expect(SAG_SETTINGS_NAMESPACE).toBe('dsh-sag')
  })

})
