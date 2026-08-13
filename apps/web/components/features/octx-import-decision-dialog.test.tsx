import * as React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { NextIntlClientProvider } from "next-intl";
import { describe, expect, it, vi } from "vitest";

import messages from "@/messages/zh-CN.json";
import { AlertDialog } from "@/components/ui/alert-dialog";
import {
  buildImportDecisionRequest,
  OctxImportDecisionPanel,
} from "./octx-import-decision-dialog";

describe("OCTX import decision dialog", () => {
  it("defaults to the safer create-new choice with SAG-native hierarchy", () => {
    const html = renderToStaticMarkup(
      <NextIntlClientProvider locale="zh-CN" timeZone="Asia/Shanghai" messages={messages}>
        <AlertDialog open><OctxImportDecisionPanel
          sourceName="AI"
          hasLocalChanges={false}
          allowUpdate
          selectedAction="new"
          discard={false}
          submitting={false}
          onSelect={vi.fn()}
          onDiscardChange={vi.fn()}
          onCancel={vi.fn()}
          onContinue={vi.fn()}
        /></AlertDialog>
      </NextIntlClientProvider>,
    );

    expect(html).toContain("检测到相同的 OCTX 信源");
    expect(html).toContain("现有信源");
    expect(html).toContain("推荐");
    expect(html).toContain('role="radio"');
    expect(html).toContain('aria-checked="true"');
    expect(html).toContain("继续");
  });

  it("keeps local-change confirmation inside the update choice", () => {
    const html = renderToStaticMarkup(
      <NextIntlClientProvider locale="zh-CN" timeZone="Asia/Shanghai" messages={messages}>
        <AlertDialog open><OctxImportDecisionPanel
          sourceName="AI"
          hasLocalChanges
          allowUpdate
          selectedAction="update"
          discard={false}
          submitting={false}
          onSelect={vi.fn()}
          onDiscardChange={vi.fn()}
          onCancel={vi.fn()}
          onContinue={vi.fn()}
        /></AlertDialog>
      </NextIntlClientProvider>,
    );

    expect(html).toContain("放弃现有信源中的本地修改");
    expect(html).toContain("disabled");
  });

  it("does not offer update when the backend disallows it", () => {
    const html = renderToStaticMarkup(
      <NextIntlClientProvider locale="zh-CN" timeZone="Asia/Shanghai" messages={messages}>
        <AlertDialog open><OctxImportDecisionPanel
          sourceName="AI"
          hasLocalChanges={false}
          allowUpdate={false}
          selectedAction="new"
          discard={false}
          submitting={false}
          onSelect={vi.fn()}
          onDiscardChange={vi.fn()}
          onCancel={vi.fn()}
          onContinue={vi.fn()}
        /></AlertDialog>
      </NextIntlClientProvider>,
    );

    expect(html).not.toContain("更新现有信源");
  });

  it("builds unchanged backend decision payloads", () => {
    expect(buildImportDecisionRequest("new", "signed", "source-1", false)).toEqual({
      action: "new",
      decision_token: "signed",
    });
    expect(buildImportDecisionRequest("update", "signed", "source-1", true)).toEqual({
      action: "update",
      decision_token: "signed",
      target_source_id: "source-1",
      discard_local_changes: true,
    });
  });
});
