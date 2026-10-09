import type { SagConnectionDescriptor } from './types.js'

const descriptorFields = new Set([
  'schemaVersion',
  'name',
  'apiUrl',
  'mcpUrl',
  'accessToken',
  'defaultSourceId',
])

function isPlainObject(value: unknown): value is Record<string, unknown> {
  if (typeof value !== 'object' || value === null) return false
  const prototype = Object.getPrototypeOf(value)
  return prototype === Object.prototype || prototype === null
}

function requiredString(object: Record<string, unknown>, field: string): string {
  const value = object[field]
  if (typeof value !== 'string' || !value.trim()) {
    throw new Error(`dsh-sag: connection descriptor ${field} must be a non-empty string`)
  }
  return value
}

function httpUrl(object: Record<string, unknown>, field: string): URL {
  const value = requiredString(object, field)
  let parsed: URL
  try {
    parsed = new URL(value)
  } catch {
    throw new Error(`dsh-sag: connection descriptor ${field} must be an http or https URL`)
  }
  if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') {
    throw new Error(`dsh-sag: connection descriptor ${field} must be an http or https URL`)
  }
  if (parsed.username || parsed.password) {
    throw new Error(`dsh-sag: connection descriptor ${field} must not contain a username or password`)
  }
  return parsed
}

/** Parse a complete, versioned connection descriptor from a JSON or file boundary. */
export function parseConnectionDescriptor(value: unknown): SagConnectionDescriptor {
  if (!isPlainObject(value)) throw new Error('dsh-sag: connection descriptor must be a plain object')
  for (const field of Object.keys(value)) {
    if (!descriptorFields.has(field)) {
      throw new Error(`dsh-sag: connection descriptor has unknown field ${field}`)
    }
  }
  if (value.schemaVersion !== 1) {
    throw new Error('dsh-sag: connection descriptor requires schemaVersion 1')
  }

  const defaultSourceId = value.defaultSourceId
  if (defaultSourceId !== undefined && defaultSourceId !== null && (typeof defaultSourceId !== 'string' || !defaultSourceId.trim())) {
    throw new Error('dsh-sag: connection descriptor defaultSourceId must be a non-empty string or null')
  }

  const apiUrl = httpUrl(value, 'apiUrl')
  const mcpUrl = httpUrl(value, 'mcpUrl')
  return {
    schemaVersion: 1,
    name: requiredString(value, 'name'),
    apiUrl: apiUrl.toString().replace(/\/$/, ''),
    mcpUrl: mcpUrl.toString(),
    accessToken: requiredString(value, 'accessToken'),
    ...(defaultSourceId === undefined ? {} : { defaultSourceId }),
  }
}
