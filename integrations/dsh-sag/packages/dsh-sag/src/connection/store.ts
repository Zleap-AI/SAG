import type { Context, Volatile } from '@deepseek-ai/cordis'
import { credentialKey, type CredentialProvider, type CredentialRecord } from '@deepseek-ai/dsh-credentials'
import type { SettingsNamespace } from '@deepseek-ai/dsh-settings'
import { DEFAULT_CONNECTION_SETTINGS } from '../config.js'
import { parseConnectionDescriptor } from './descriptor.js'
import type { SagConnectionDescriptor, SagCredentialPayload, SagLocalSettings } from './types.js'

/** The fixed settings namespace; its brand is type-only so host loading needs no helper export. */
export const SAG_SETTINGS_NAMESPACE = 'dsh-sag' as SettingsNamespace

/** The only credential record owned by the local SAG connection. */
export const SAG_CREDENTIAL_KEY = credentialKey('dsh-sag', 'local')

/** Machine-readable error for saved connection data that cannot be used safely. */
export class SagConnectionConfigurationError extends Error {
  /** Stable error code for interactive configuration surfaces. */
  readonly code = 'SAG_CONNECTION_CONFIG_INVALID'

  /** Stored location that failed validation. */
  readonly field: string

  /**
   * @param field - stored location that failed validation.
   * @param message - human-readable validation failure.
   */
  constructor(field: string, message: string) {
    super(`dsh-sag: saved connection ${field} ${message}`)
    this.name = 'SagConnectionConfigurationError'
    this.field = field
  }
}

/** The services the store uses; kept small so callers can provide real providers or focused in-memory fakes. */
export interface SagConnectionStoreDeps {
  readonly credentials: Pick<CredentialProvider, 'readRecord' | 'modifyRecord' | 'deleteRecord'>
  readonly settings: SagConnectionSettings
}

/** Non-secret persistence used by either SettingsForms or the standalone profile editor. */
export interface SagConnectionSettings {
  get(): SagLocalSettings | Promise<SagLocalSettings>
  update(patch: object): Promise<void>
  replace(next: object): Promise<void>
}

/** Adapt a plugin Config reference to the active profile's SettingsForms entry. */
export function registerSagSettings(
  ctx: Context,
  connection: SagLocalSettings | Volatile<SagLocalSettings> = DEFAULT_CONNECTION_SETTINGS,
  namespace: string = SAG_SETTINGS_NAMESPACE,
): SagConnectionSettings {
  return {
    get: () => 'get' in connection ? connection.get() : connection,
    update: patch => ctx.settings.update(namespace, { connection: patch }),
    replace: next => ctx.settings.update(namespace, { connection: { ...DEFAULT_CONNECTION_SETTINGS, ...next } }),
  }
}

function plainObject(value: unknown): value is Record<string, unknown> {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return false
  const prototype = Object.getPrototypeOf(value)
  return prototype === Object.prototype || prototype === null
}

function localSettings(value: unknown): SagLocalSettings {
  if (!plainObject(value)) {
    throw new SagConnectionConfigurationError('settings', 'must be an object')
  }
  const expected = new Set(['schemaVersion', 'mode', 'name', 'apiUrl', 'mcpUrl', 'defaultSourceId', 'credentialId'])
  for (const key of Object.keys(value)) {
    if (!expected.has(key)) {
      throw new SagConnectionConfigurationError(`settings.${key}`, 'is not supported')
    }
  }
  if (value.schemaVersion !== 1) {
    throw new SagConnectionConfigurationError('settings.schemaVersion', 'must be 1')
  }
  if (value.mode !== 'local') {
    throw new SagConnectionConfigurationError('settings.mode', 'must be local')
  }
  if (value.credentialId !== 'local') {
    throw new SagConnectionConfigurationError('settings.credentialId', 'must be local')
  }
  for (const field of ['name', 'apiUrl', 'mcpUrl']) {
    if (typeof value[field] !== 'string') {
      throw new SagConnectionConfigurationError(`settings.${field}`, 'must be a string')
    }
  }
  const defaultSourceId = value.defaultSourceId
  if (defaultSourceId !== null && (typeof defaultSourceId !== 'string' || !defaultSourceId.trim())) {
    throw new SagConnectionConfigurationError('settings.defaultSourceId', 'must be a non-empty string or null')
  }
  return {
    schemaVersion: 1,
    mode: 'local',
    name: value.name as string,
    apiUrl: value.apiUrl as string,
    mcpUrl: value.mcpUrl as string,
    defaultSourceId,
    credentialId: 'local',
  }
}

function credentialPayload(record: CredentialRecord): SagCredentialPayload {
  if (record.kind !== 'grant') {
    throw new SagConnectionConfigurationError('credential.kind', 'must be grant')
  }
  if (!plainObject(record.payload)) {
    throw new SagConnectionConfigurationError('credential.payload', 'must be an object')
  }
  const expected = new Set(['schemaVersion', 'accessToken'])
  for (const key of Object.keys(record.payload)) {
    if (!expected.has(key)) {
      throw new SagConnectionConfigurationError(`credential.payload.${key}`, 'is not supported')
    }
  }
  if (record.payload.schemaVersion !== 1) {
    throw new SagConnectionConfigurationError('credential.payload.schemaVersion', 'must be 1')
  }
  if (typeof record.payload.accessToken !== 'string' || !record.payload.accessToken.trim()) {
    throw new SagConnectionConfigurationError('credential.payload.accessToken', 'must be a non-empty string')
  }
  return { schemaVersion: 1, accessToken: record.payload.accessToken }
}

function descriptorErrorField(error: unknown): string {
  const message = error instanceof Error ? error.message : ''
  for (const field of ['name', 'apiUrl', 'mcpUrl', 'defaultSourceId']) {
    if (message.startsWith(`dsh-sag: connection descriptor ${field} `)) {
      return `settings.${field}`
    }
  }
  return 'settings'
}

/** Store one local connection with endpoints in Settings and its bearer token in Credentials. */
export class SagConnectionStore {
  /**
   * @param deps - live settings and credential providers.
   */
  constructor(private readonly deps: SagConnectionStoreDeps) {}

  /** Read the saved connection, resolving the credential record again for every call. */
  async load(): Promise<SagConnectionDescriptor | undefined> {
    const record = await this.deps.credentials.readRecord(SAG_CREDENTIAL_KEY)
    if (record === undefined) return undefined
    const settings = localSettings(await this.deps.settings.get())
    const credential = credentialPayload(record)
    try {
      return parseConnectionDescriptor({
        schemaVersion: settings.schemaVersion,
        name: settings.name,
        apiUrl: settings.apiUrl,
        mcpUrl: settings.mcpUrl,
        accessToken: credential.accessToken,
        defaultSourceId: settings.defaultSourceId,
      })
    } catch (error) {
      throw new SagConnectionConfigurationError(
        descriptorErrorField(error),
        error instanceof Error ? error.message : 'is invalid',
      )
    }
  }

  /** Persist a complete connection, deliberately keeping its token outside Settings. */
  async save(descriptor: SagConnectionDescriptor): Promise<void> {
    const previous = await this.deps.settings.get()
    await this.deps.settings.update({
      schemaVersion: 1,
      mode: 'local',
      name: descriptor.name,
      apiUrl: descriptor.apiUrl,
      mcpUrl: descriptor.mcpUrl,
      defaultSourceId: descriptor.defaultSourceId ?? null,
      credentialId: 'local',
    })
    try {
      await this.deps.credentials.modifyRecord(SAG_CREDENTIAL_KEY, async () => ({
        kind: 'grant',
        payload: { schemaVersion: 1, accessToken: descriptor.accessToken },
      }))
    } catch (error) {
      try { await this.deps.settings.replace(previous) } catch (rollback) {
        throw new AggregateError([error, rollback], 'dsh-sag: credential save and settings rollback failed')
      }
      throw error
    }
  }

  /** Remove the saved local connection and its owned credential record. */
  async clear(): Promise<void> {
    await this.deps.credentials.deleteRecord(SAG_CREDENTIAL_KEY)
    await this.deps.settings.replace({})
  }
}
