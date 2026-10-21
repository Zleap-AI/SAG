import { setTimeout as delay } from "node:timers/promises";
import type { WorkspaceProfile } from "./workspace-profiles";

export interface HostWindow {
  window: unknown;
  load(): void;
  show(): void;
  hide(): void;
  destroy(): void;
  isDestroyed(): boolean;
}
interface HostAdapter {
  create(profile: WorkspaceProfile): HostWindow;
  probe(profile: WorkspaceProfile, signal: AbortSignal): Promise<boolean>;
}
interface HostContext { profile: WorkspaceProfile; handle: HostWindow; abort: AbortController }

/** Keeps the vendor's normal browser bootstrap alive, without implementing its token protocol. */
export class FnOsHostContext {
  private current: HostContext | undefined;
  private readonly adapter: HostAdapter;
  private readonly options: { attempts: number; pollMs: number; timeoutMs: number };
  constructor(adapter: HostAdapter, options: { attempts?: number; pollMs?: number; timeoutMs?: number } = {}) {
    this.adapter = adapter;
    this.options = { attempts: 40, pollMs: 500, timeoutMs: 20_000, ...options };
  }
  private matches(context: HostContext, profile: WorkspaceProfile): boolean {
    return !context.abort.signal.aborted && !context.handle.isDestroyed()
      && context.profile.id === profile.id && context.profile.partition === profile.partition
      && context.profile.webOrigin === profile.webOrigin;
  }
  private ensure(profile: WorkspaceProfile): HostContext {
    if (profile.kind !== "remote-web" || profile.deployment !== "fnos") throw new Error("仅 fnOS 连接支持宿主会话");
    if (this.current && this.matches(this.current, profile)) return this.current;
    this.close();
    const context = { profile, handle: this.adapter.create(profile), abort: new AbortController() };
    this.current = context;
    context.handle.load();
    return context;
  }
  show(profile: WorkspaceProfile): void { this.ensure(profile).handle.show(); }
  hide(profile: WorkspaceProfile): void {
    if (this.current && this.matches(this.current, profile)) this.current.handle.hide();
  }
  isCurrent(window: unknown, profile: WorkspaceProfile): boolean {
    return !!this.current && this.current.handle.window === window && this.matches(this.current, profile);
  }
  closed(window: unknown): void {
    const context = this.current;
    if (!context || context.handle.window !== window) return;
    context.abort.abort();
    this.current = undefined;
  }
  async ready(profile: WorkspaceProfile): Promise<boolean> {
    const context = this.ensure(profile);
    context.handle.show();
    const signal = AbortSignal.any([context.abort.signal, AbortSignal.timeout(this.options.timeoutMs)]);
    for (let attempt = 0; attempt < this.options.attempts && !signal.aborted; attempt++) {
      if (this.current !== context || !this.matches(context, profile)) return false;
      try {
        if (await this.adapter.probe(profile, signal)) {
          return !signal.aborted && this.current === context && this.matches(context, profile);
        }
      } catch { /* The normal host page may still be reconnecting or awaiting login. */ }
      if (attempt + 1 < this.options.attempts && !signal.aborted) {
        try { await delay(this.options.pollMs, undefined, { signal }); } catch { return false; }
      }
    }
    return false;
  }
  close(): void {
    const context = this.current;
    this.current = undefined;
    if (!context) return;
    context.abort.abort();
    if (!context.handle.isDestroyed()) context.handle.destroy();
  }
  closeProfile(id: string): void {
    if (this.current?.profile.id === id) this.close();
  }
}
