import * as React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { NextIntlClientProvider } from "next-intl";
import { describe, expect, it, vi } from "vitest";

import { runSourceIdCopy, SourceIdCopy } from "./source-id-copy";

describe("source id copy", () => {
  it("copies the complete source id and reports success", async () => {
    const copy = vi.fn(async () => undefined);
    const onSuccess = vi.fn();
    const onFailure = vi.fn();

    await runSourceIdCopy({
      sourceId: "3fe9533639544615bc732d8d7a8f648e",
      copy,
      onSuccess,
      onFailure,
    });

    expect(copy).toHaveBeenCalledWith("3fe9533639544615bc732d8d7a8f648e");
    expect(onSuccess).toHaveBeenCalledOnce();
    expect(onFailure).not.toHaveBeenCalled();
  });

  it("reports clipboard failures without reporting success", async () => {
    const onSuccess = vi.fn();
    const onFailure = vi.fn();

    await runSourceIdCopy({
      sourceId: "source-a",
      copy: async () => {
        throw new Error("denied");
      },
      onSuccess,
      onFailure,
    });

    expect(onSuccess).not.toHaveBeenCalled();
    expect(onFailure).toHaveBeenCalledOnce();
  });

  it("renders a localized, accessible control with the complete id available", () => {
    const html = renderToStaticMarkup(
      <NextIntlClientProvider
        locale="zh-CN"
        messages={{
          Knowledge: {
            sourceId: "信源 ID",
            copySourceId: "复制信源 ID",
            sourceIdCopied: "信源 ID 已复制",
            sourceIdCopyFailed: "信源 ID 复制失败",
          },
        }}
      >
        <SourceIdCopy sourceId="3fe9533639544615bc732d8d7a8f648e" />
      </NextIntlClientProvider>,
    );

    expect(html).toContain("信源 ID");
    expect(html).toContain("3fe9533639544615bc732d8d7a8f648e");
    expect(html).toContain('aria-label="复制信源 ID"');
    expect(html).toContain('title="3fe9533639544615bc732d8d7a8f648e"');
  });
});
