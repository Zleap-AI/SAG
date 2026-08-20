import { SAG_KNOWLEDGE_MCP_SERVER_KEY } from "./mcp-server-key";
import type { KnowledgeMcpDescriptor } from "./types";

type StandardMcpServerConfig = {
  type: "http";
  transport: string;
  url: string;
  headers: Record<string, string>;
};

export type StandardMcpConfig = {
  mcpServers: Record<string, StandardMcpServerConfig>;
};

export function mcpHttpUrl(descriptor: KnowledgeMcpDescriptor, origin: string): string {
  if (descriptor.mode === "fnos") {
    const publicUrl = new URL(origin);
    publicUrl.protocol = "http:";
    publicUrl.port = "15167";
    publicUrl.pathname = "/mcp/";
    publicUrl.search = "";
    publicUrl.hash = "";
    return publicUrl.toString();
  }
  if (descriptor.http.url) return descriptor.http.url;
  if (descriptor.http.path) return new URL(descriptor.http.path, origin).toString();
  throw new Error("MCP HTTP endpoint is unavailable");
}

export function buildStandardMcpConfig(
  descriptor: KnowledgeMcpDescriptor,
  token: string,
  origin: string,
): StandardMcpConfig {
  return {
    mcpServers: {
      [SAG_KNOWLEDGE_MCP_SERVER_KEY]: {
        type: "http",
        transport: descriptor.http.transport.replace("-", "_"),
        url: mcpHttpUrl(descriptor, origin),
        headers: { ...descriptor.http.headers, Authorization: `Bearer ${token}` },
      },
    },
  };
}

export function buildHermesFormUrl(
  descriptor: KnowledgeMcpDescriptor,
  token: string,
  origin: string,
): string {
  const url = new URL(mcpHttpUrl(descriptor, origin));
  url.searchParams.set("token", token);
  return url.toString();
}
