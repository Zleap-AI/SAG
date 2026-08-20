import { describe, expect, it } from "vitest";

import {
  buildHermesFormUrl,
  buildStandardMcpConfig,
} from "./mcp-quick-connect";
import type { KnowledgeMcpDescriptor } from "./types";

const descriptor: KnowledgeMcpDescriptor = {
  name: "SAG 知识库",
  scope: "knowledge_base",
  mode: "fnos",
  source_count: 2,
  tools: ["search_knowledge"],
  tool_details: [],
  grants: [],
  http: {
    transport: "streamable-http",
    path: "/app/sag/mcp/",
    headers: { Authorization: "Bearer <SAG_FNOS_MCP_TOKEN>" },
    note: "credential required",
  },
};

describe("fnOS MCP quick connect", () => {
  it("builds a standard Streamable HTTP config without putting the token in the URL", () => {
    const config = buildStandardMcpConfig(descriptor, "sagf_mcp_secret", "http://nas.local:5666");

    expect(config).toEqual({
      mcpServers: {
        "sag-knowledge": {
          type: "http",
          transport: "streamable_http",
          url: "http://nas.local:15167/mcp/",
          headers: { Authorization: "Bearer sagf_mcp_secret" },
        },
      },
    });
    expect(JSON.stringify(config)).not.toContain("mcp_secret@")
  });

  it("builds a Hermes dashboard URL that can be pasted into its HTTP/SSE form", () => {
    const url = buildHermesFormUrl(descriptor, "sagf_mcp_secret", "http://nas.local:5666");
    expect(url).toBe("http://nas.local:15167/mcp/?token=sagf_mcp_secret");
  });
});
