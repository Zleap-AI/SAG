export const SAG_KNOWLEDGE_MCP_SERVER_KEY = "sag-knowledge";

const INVALID_MCP_SERVER_KEY_CHARACTER = /[^a-zA-Z0-9_-]+/g;
const EDGE_SEPARATOR = /^-+|-+$/g;

function sanitizeMcpServerKey(value: string): string {
  return value
    .normalize("NFKD")
    .replace(/[\u0300-\u036f]/g, "")
    .trim()
    .replace(INVALID_MCP_SERVER_KEY_CHARACTER, "-")
    .replace(EDGE_SEPARATOR, "");
}

export function mcpServerKey(label: string, fallback = SAG_KNOWLEDGE_MCP_SERVER_KEY): string {
  return sanitizeMcpServerKey(label) || sanitizeMcpServerKey(fallback) || "sag";
}
