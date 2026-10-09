/** A versioned local SAG connection, as exported by the SAG application. */
export interface SagConnectionDescriptor {
  readonly schemaVersion: 1
  readonly name: string
  readonly apiUrl: string
  readonly mcpUrl: string
  readonly accessToken: string
  readonly defaultSourceId?: string | null
}

/** The non-sensitive portion of a saved local SAG connection. */
export interface SagLocalSettings {
  readonly schemaVersion: 1
  readonly mode: 'local'
  readonly name: string
  readonly apiUrl: string
  readonly mcpUrl: string
  readonly defaultSourceId?: string | null
  readonly credentialId: 'local'
}

/** The credential payload kept separately from local settings. */
export interface SagCredentialPayload {
  readonly schemaVersion: 1
  readonly accessToken: string
}

/** The versioned dsh integration capabilities advertised by SAG. */
export interface SagCapabilityDescriptor {
  readonly schemaVersion: 1
  readonly capabilities: readonly string[]
  readonly upload?: {
    readonly maxMb: number
    readonly extensions: readonly string[]
  }
  readonly defaultSourceId?: string | null
}
