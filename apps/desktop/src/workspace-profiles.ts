import { randomUUID } from "node:crypto";
import { existsSync, mkdirSync, readFileSync, renameSync, writeFileSync } from "node:fs";
import path from "node:path";

export interface WorkspaceProfile {
  id: string;
  name: string;
  kind: "local-managed" | "remote-web";
  deployment: "standard" | "fnos";
  webOrigin: string;
  appPrefix: string;
  partition: string;
}
export interface WorkspaceState { schemaVersion: 1; activeId: string | null; profiles: WorkspaceProfile[] }
export interface RemoteProfileInput { id?: string; name: string; webUrl: string; deployment: "standard" | "fnos" }
const local: WorkspaceProfile = { id: "local", name: "本机知识库", kind: "local-managed", deployment: "standard",
  webOrigin: "", appPrefix: "", partition: "" };

export function parseWorkspaceUrl(value: string): { webOrigin: string; appPrefix: string } {
  if (typeof value !== "string" || value.length > 2000 || /[\\\s]/.test(value) || /%(?:2e|2f|5c)/i.test(value)) {
    throw new Error("请输入完整的 HTTP 或 HTTPS 工作台地址");
  }
  const url = new URL(value);
  if (!["https:", "http:"].includes(url.protocol) || url.username || url.password || url.search || url.hash ||
      value.split("/").includes("..") || value.split("/").includes(".")) {
    throw new Error("工作台地址不能包含凭据、查询参数或路径跳转");
  }
  return { webOrigin: url.origin, appPrefix: url.pathname.replace(/\/+$/, "") };
}

export class WorkspaceProfiles {
  private readonly file: string;
  private readonly directory: string;
  constructor(directory: string) { this.directory = directory; this.file = path.join(directory, "workspace-profiles.json"); }

  read(): WorkspaceState {
    if (!existsSync(this.file)) return { schemaVersion: 1, activeId: "local", profiles: [{ ...local }] };
    const value = JSON.parse(readFileSync(this.file, "utf8")) as WorkspaceState;
    if (value.schemaVersion !== 1 || !Array.isArray(value.profiles) || value.profiles.length > 100) {
      throw new Error("工作区配置版本不受支持，请保留配置并升级 SAG");
    }
    const ids = new Set<string>(); const partitions = new Set<string>();
    for (const profile of value.profiles) {
      if (!profile || typeof profile.id !== "string" || ids.has(profile.id) ||
          typeof profile.name !== "string" || profile.name.length > 120) throw new Error("工作区配置无效");
      ids.add(profile.id);
      if (profile.kind === "local-managed") {
        if (JSON.stringify(profile) !== JSON.stringify(local)) throw new Error("本机工作区配置无效");
      } else if (profile.kind === "remote-web") {
        const normalized = parseWorkspaceUrl(profile.webOrigin + profile.appPrefix);
        if (normalized.webOrigin !== profile.webOrigin || normalized.appPrefix !== profile.appPrefix ||
            !/^persist:sag-workspace-[a-f0-9-]{36}$/.test(profile.partition) || partitions.has(profile.partition) ||
            !["fnos", "standard"].includes(profile.deployment)) throw new Error("远程工作区配置无效");
        partitions.add(profile.partition);
      } else throw new Error("未知工作区类型");
    }
    if (!ids.has("local") || (value.activeId !== null && !ids.has(value.activeId))) throw new Error("活动工作区无效");
    return value;
  }
  private write(state: WorkspaceState): void {
    mkdirSync(this.directory, { recursive: true });
    const temporary = `${this.file}.${randomUUID()}.tmp`;
    writeFileSync(temporary, JSON.stringify(state, null, 2), { mode: 0o600 });
    renameSync(temporary, this.file);
  }
  save(input: RemoteProfileInput): WorkspaceProfile {
    if (!input || typeof input.name !== "string" || !input.name.trim() || input.name.length > 120 ||
        !["fnos", "standard"].includes(input.deployment)) throw new Error("请填写连接名称与服务器类型");
    const state = this.read(); const endpoint = parseWorkspaceUrl(input.webUrl);
    const previous = state.profiles.find(profile => profile.id === input.id);
    if (input.id && (!previous || previous.kind !== "remote-web")) throw new Error("连接不存在");
    if (!previous && state.profiles.length >= 100) throw new Error("已达到连接数量上限");
    const sameEndpoint = previous?.webOrigin === endpoint.webOrigin && previous.appPrefix === endpoint.appPrefix;
    const profile: WorkspaceProfile = { id: previous?.id ?? randomUUID(), kind: "remote-web", name: input.name.trim(),
      deployment: input.deployment, ...endpoint,
      partition: sameEndpoint ? previous.partition : `persist:sag-workspace-${randomUUID()}` };
    state.profiles = [...state.profiles.filter(item => item.id !== profile.id), profile]; this.write(state);
    return profile;
  }
  activate(id: string): void {
    const state = this.read(); if (!state.profiles.some(profile => profile.id === id)) throw new Error("连接不存在");
    state.activeId = id; this.write(state);
  }
  remove(id: string): WorkspaceProfile {
    const state = this.read(); const profile = state.profiles.find(item => item.id === id);
    if (!profile || profile.kind !== "remote-web") throw new Error("不能删除本机工作区");
    state.profiles = state.profiles.filter(item => item.id !== id);
    if (state.activeId === id) state.activeId = null;
    this.write(state); return profile;
  }
}
