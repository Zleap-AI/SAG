export function hasRecordedTokenUsage(tokenUsage: number): boolean {
  return Number.isFinite(tokenUsage) && tokenUsage > 0;
}
