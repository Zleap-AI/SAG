// @vitest-environment jsdom

import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { NextIntlClientProvider } from "next-intl";
import { beforeEach, expect, it, vi } from "vitest";

import messages from "@/messages/zh-CN.json";
import { api } from "@/lib/api";
import type { KnowledgeMcpDescriptor } from "@/lib/types";

import { McpServiceSettings } from "./mcp-service-settings";

vi.mock("@/lib/api", () => ({
  api: {
    knowledgeMcp: vi.fn(),
    issueFnOSMcpGrant: vi.fn(),
    revokeFnOSMcpGrant: vi.fn(),
    deleteFnOSMcpGrantRecord: vi.fn(),
  },
  ApiError: class ApiError extends Error {},
}));

const descriptor: KnowledgeMcpDescriptor = {
  name: "SAG 知识库",
  scope: "knowledge_base",
  mode: "fnos",
  source_count: 1,
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

beforeEach(() => {
  vi.mocked(api.knowledgeMcp).mockResolvedValue(descriptor);
  vi.mocked(api.issueFnOSMcpGrant).mockResolvedValue({
    id: "grant-1",
    token: "sagf_mcp_secret",
    expires_in_days: 7,
    expires_at: "2026-08-27T00:00:00Z",
    created_at: "2026-08-20T00:00:00Z",
    revoked_at: null,
  });
});

it("shows paste-ready Hermes form values instead of a desktop-only import link", async () => {
  const user = userEvent.setup();
  render(
    <NextIntlClientProvider locale="zh-CN" messages={messages}>
      <McpServiceSettings />
    </NextIntlClientProvider>,
  );

  await user.click(await screen.findByRole("button", { name: "生成凭据" }));

  await waitFor(() => expect(screen.getByRole("button", { name: "复制 URL" })).toBeVisible());
  expect(screen.getByText("HTTP/SSE")).toBeVisible();
  expect(screen.getAllByText(/http:\/\/localhost:15167\/mcp\//).length).toBeGreaterThan(0);
  expect(screen.queryByRole("link", { name: "在 Hermes 中添加" })).not.toBeInTheDocument();
  expect(screen.queryByText("sagf_mcp_secret")).not.toBeInTheDocument();
});

it("keeps invalid grants collapsed and lets the user delete their record", async () => {
  const user = userEvent.setup();
  vi.mocked(api.knowledgeMcp).mockResolvedValueOnce({
    ...descriptor,
    grants: [],
    inactive_grants: [{
      id: "revoked-grant",
      expires_at: "2026-08-27T00:00:00Z",
      created_at: "2026-08-20T00:00:00Z",
      revoked_at: "2026-08-21T00:00:00Z",
    }],
  });
  vi.mocked(api.deleteFnOSMcpGrantRecord).mockResolvedValue(undefined);
  render(
    <NextIntlClientProvider locale="zh-CN" messages={messages}>
      <McpServiceSettings />
    </NextIntlClientProvider>,
  );

  await screen.findByRole("button", { name: "已失效授权（1）" });
  expect(screen.queryByText("已撤销，需要重新授权")).not.toBeInTheDocument();

  await user.click(screen.getByRole("button", { name: "已失效授权（1）" }));
  expect(await screen.findByText("已撤销，需要重新授权")).toBeVisible();
  await user.click(screen.getByRole("button", { name: "删除记录" }));
  expect(api.deleteFnOSMcpGrantRecord).toHaveBeenCalledWith("revoked-grant");
});
