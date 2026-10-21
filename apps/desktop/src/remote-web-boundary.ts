export interface RemoteWindowBinding {
  window: unknown;
  profileId: string | undefined;
  partition: string | undefined;
  session: unknown;
}

/** No host authorization-popup contract is installed. Never infer one from a state parameter. */
export class RemoteWebBoundary {
  private readonly origin: string;
  private readonly binding: RemoteWindowBinding;
  constructor(origin: string, binding: RemoteWindowBinding) { this.origin = origin; this.binding = binding; }
  isCurrent(current: RemoteWindowBinding): boolean {
    return current.window === this.binding.window && current.session === this.binding.session &&
      current.profileId === this.binding.profileId && current.partition === this.binding.partition;
  }
  navigationAllowed(url: string, current: RemoteWindowBinding): boolean {
    try { return this.isCurrent(current) && new URL(url).origin === this.origin; }
    catch { return false; }
  }
  popupDisposition(current: RemoteWindowBinding): "unsupported" | "stale" {
    return this.isCurrent(current) ? "unsupported" : "stale";
  }
}
