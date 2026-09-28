// @vitest-environment jsdom
import * as React from "react";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import messages from "@/messages/zh-CN.json";
import { api } from "@/lib/api";
import type { FnOSKnowledgeUpgradeStatus } from "@/lib/types";
import { FnOSKnowledgeUpgradeBanner, FnOSKnowledgeUpgradePanel, FnOSKnowledgeUpgradeProvider } from "./fnos-knowledge-upgrade-panel";
vi.mock("@/lib/api", () => ({ api: { fnosKnowledgeUpgradeStatus: vi.fn(), fnosReingestKnowledge: vi.fn(), fnosReplaceMissingOriginal: vi.fn() } }));
vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn() } }));
const pending: FnOSKnowledgeUpgradeStatus = { required: true, total: 3, states: { pending: 1, needs_file: 1, queued: 0, running: 0, ready: 1, failed: 0 }, missing: [{ source_id: "a", document_id: "d", filename: "missing.md" }] };
function ui(enabled = true, identity = "alice") {
  return <NextIntlClientProvider locale="zh-CN" timeZone="Asia/Shanghai" messages={messages}>
    <FnOSKnowledgeUpgradeProvider key={identity} enabled={enabled}>
      <FnOSKnowledgeUpgradeBanner />
      <FnOSKnowledgeUpgradePanel sourceId="a" onChanged={vi.fn()} />
    </FnOSKnowledgeUpgradeProvider>
  </NextIntlClientProvider>;
}
beforeEach(() => { vi.clearAllMocks(); vi.mocked(api.fnosKnowledgeUpgradeStatus).mockResolvedValue(pending); vi.mocked(api.fnosReingestKnowledge).mockResolvedValue({ queued: 1, needs_file: 1 }); });
afterEach(() => vi.useRealTimers());
describe("Native upgrade guidance", () => {
  it("does not invalidate a response slower than the polling interval", async () => {
    vi.useFakeTimers();
    let resolve!: (status: FnOSKnowledgeUpgradeStatus) => void;
    vi.mocked(api.fnosKnowledgeUpgradeStatus).mockImplementation(() => new Promise((done) => { resolve = done; }));
    render(ui());
    await act(async () => { await vi.advanceTimersByTimeAsync(11_000); });
    expect(api.fnosKnowledgeUpgradeStatus).toHaveBeenCalledTimes(1);
    await act(async () => { resolve(pending); });
    expect(screen.getByRole("link", { name: "查看知识库并重新入库" })).toBeVisible();
  });
  it("shares one poll and shows global warning without starting model work", async () => {
    render(ui());
    expect(await screen.findByRole("link", { name: "查看知识库并重新入库" })).toHaveAttribute("href", "/knowledge");
    expect(screen.getAllByText(/可能产生模型费用/)).toHaveLength(2);
    expect(api.fnosKnowledgeUpgradeStatus).toHaveBeenCalledTimes(1);
    expect(api.fnosReingestKnowledge).not.toHaveBeenCalled();
  });
  it("shows completion instead of stale warning or reingest button", async () => {
    vi.mocked(api.fnosKnowledgeUpgradeStatus).mockResolvedValue({ ...pending, required: false, missing: [], states: { ...pending.states, pending: 0, needs_file: 0, ready: 3 } });
    render(ui());
    expect(await screen.findByText("旧知识已重新入库完成")).toBeVisible();
    expect(screen.queryByText("旧知识需要重新入库")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "重新入库我的旧知识" })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "查看知识库并重新入库" })).not.toBeInTheDocument();
  });
  it("shows retry on initial failure and recovers", async () => {
    vi.mocked(api.fnosKnowledgeUpgradeStatus).mockRejectedValueOnce(new Error("503")); render(ui());
    await screen.findAllByText("暂时无法读取知识升级状态，请重试。");
    fireEvent.click(screen.getAllByRole("button", { name: "重试" })[0]);
    expect(await screen.findByRole("link", { name: "查看知识库并重新入库" })).toBeVisible();
  });
  it("keeps last warning after refresh failure and blocks stale actions", async () => {
    render(ui()); await screen.findByRole("button", { name: "重新入库我的旧知识" });
    vi.mocked(api.fnosKnowledgeUpgradeStatus).mockRejectedValue(new Error("503"));
    fireEvent.click(screen.getByRole("button", { name: "重新入库我的旧知识" }));
    await screen.findAllByText("暂时无法读取知识升级状态，请重试。");
    expect(screen.getAllByText("旧知识需要重新入库")).toHaveLength(2);
    expect(screen.getByRole("button", { name: "重新入库我的旧知识" })).toBeDisabled();
  });
  it("makes no request outside authenticated Native session", () => { render(ui(false)); expect(api.fnosKnowledgeUpgradeStatus).not.toHaveBeenCalled(); expect(screen.queryByRole("alert")).not.toBeInTheDocument(); });
  it("hides guidance for fresh tenant", async () => { vi.mocked(api.fnosKnowledgeUpgradeStatus).mockResolvedValue({ ...pending, required: false, total: 0, missing: [] }); render(ui()); await waitFor(() => expect(api.fnosKnowledgeUpgradeStatus).toHaveBeenCalled()); expect(screen.queryByRole("alert")).not.toBeInTheDocument(); });
  it("does not leak delayed old-tenant response after account switch", async () => {
    let resolveOld!: (status: FnOSKnowledgeUpgradeStatus) => void;
    vi.mocked(api.fnosKnowledgeUpgradeStatus).mockImplementationOnce(() => new Promise((resolve) => { resolveOld = resolve; }));
    const view = render(ui(true, "alice"));
    vi.mocked(api.fnosKnowledgeUpgradeStatus).mockResolvedValue({ ...pending, required: false, total: 0, missing: [] }); view.rerender(ui(true, "bob"));
    await waitFor(() => expect(api.fnosKnowledgeUpgradeStatus).toHaveBeenCalledTimes(2)); resolveOld(pending);
    await waitFor(() => expect(screen.queryByRole("alert")).not.toBeInTheDocument());
  });
  it("supplementing original never queues until explicit reingest click", async () => {
    vi.mocked(api.fnosReplaceMissingOriginal).mockResolvedValue({ uploaded: true }); render(ui());
    const file = new File(["text"], "missing.md", { type: "text/markdown" });
    fireEvent.change(await screen.findByLabelText("重新上传 missing.md"), { target: { files: [file] } });
    await waitFor(() => expect(api.fnosReplaceMissingOriginal).toHaveBeenCalledWith("d", file)); expect(api.fnosReingestKnowledge).not.toHaveBeenCalled();
    await waitFor(() => expect(screen.getByRole("button", { name: "重新入库我的旧知识" })).toBeEnabled());
    fireEvent.click(screen.getByRole("button", { name: "重新入库我的旧知识" })); await waitFor(() => expect(api.fnosReingestKnowledge).toHaveBeenCalledTimes(1));
  });
});
