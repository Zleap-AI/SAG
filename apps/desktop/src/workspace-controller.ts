import type { WorkspaceProfile, WorkspaceProfiles } from "./workspace-profiles";

interface LocalLifecycle {
  startLocal(): Promise<string>;
  stopLocal(): Promise<void>;
  inspectLocal(): Promise<boolean>;
  confirmSwitch(active: boolean | null): Promise<boolean>;
}
export type WorkspaceDestination = "knowledge" | "host-login";
export interface OpenWorkspace { profile: WorkspaceProfile; url: string }
export function trustedFrame(event: { sender: unknown; senderFrame: unknown }, contents: { mainFrame: unknown } | null): boolean {
  return contents !== null && event.sender === contents && event.senderFrame === contents.mainFrame;
}

/** Remote workspaces deliberately have no server lifecycle handle. */
export class WorkspaceController {
  active: WorkspaceProfile | undefined;
  private pending = false;
  private readonly profiles: WorkspaceProfiles;
  private readonly local: LocalLifecycle;
  constructor(profiles: WorkspaceProfiles, local: LocalLifecycle) { this.profiles = profiles; this.local = local; }
  async open(id: string, destination: WorkspaceDestination = "knowledge"): Promise<OpenWorkspace | null> {
    if (this.pending) throw new Error("工作区正在切换，请稍后重试");
    this.pending = true;
    try {
      let profile = this.profiles.read().profiles.find(item => item.id === id);
      if (!profile) throw new Error("连接不存在");
      const endpoint = JSON.stringify([profile.kind, profile.deployment, profile.webOrigin, profile.appPrefix, profile.partition]);
      const unchangedProfile = (): WorkspaceProfile => {
        const current = this.profiles.read().profiles.find(item => item.id === id);
        if (!current || JSON.stringify([current.kind, current.deployment, current.webOrigin, current.appPrefix, current.partition]) !== endpoint) {
          throw new Error("连接已更改或删除，请重新打开工作区");
        }
        return current;
      };
      if (destination === "host-login" && (profile.kind !== "remote-web" || profile.deployment !== "fnos")) {
        throw new Error("仅 fnOS 连接支持登录 NAS 宿主");
      }
      if (this.active?.kind === "local-managed" && profile.kind !== "local-managed") {
        const active = await this.local.inspectLocal().catch(() => null);
        if (active !== false && !await this.local.confirmSwitch(active)) return null;
        profile = unchangedProfile();
        await this.local.stopLocal();
        this.active = undefined;
      }
      profile = unchangedProfile();
      const url = profile.kind === "local-managed" ? await this.local.startLocal()
        : destination === "host-login" ? `${profile.webOrigin}/` : `${profile.webOrigin}${profile.appPrefix}/chat`;
      this.active = profile; this.profiles.activate(id);
      return { profile, url };
    } finally { this.pending = false; }
  }
  async close(): Promise<void> {
    if (this.active?.kind === "local-managed") await this.local.stopLocal();
    this.active = undefined;
  }
}
