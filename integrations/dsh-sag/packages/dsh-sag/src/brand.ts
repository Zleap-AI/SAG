/** A string whose meaning is a configured SAG namespace. */
export type SagNamespaceId = string & { readonly __brand: 'SagNamespaceId' }

/** An opaque reference returned by `sag_search` and consumed by `sag_read`. */
export type SagEvidenceRef = string & { readonly __brand: 'SagEvidenceRef' }

/** Validate and brand a namespace at a wire or configuration boundary. */
export function sagNamespaceId(value: string): SagNamespaceId {
  if (!/^[A-Za-z0-9][A-Za-z0-9._-]{0,35}$/.test(value)) {
    throw new TypeError(`invalid SAG namespace id: ${JSON.stringify(value)}`)
  }
  return value as SagNamespaceId
}

/** Validate and brand an opaque evidence reference at a wire boundary. */
export function sagEvidenceRef(value: string): SagEvidenceRef {
  if (!/^[A-Za-z0-9_-]+$/.test(value) || value.length > 4096) {
    throw new TypeError('invalid SAG evidence reference')
  }
  return value as SagEvidenceRef
}
