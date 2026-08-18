// @vitest-environment jsdom

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { expect, it, vi } from "vitest";

import messages from "@/messages/zh-CN.json";
import { api } from "@/lib/api";
import { UploadZone } from "./upload-zone";

vi.mock("@/lib/api", async (original) => {
  const actual = await original<typeof import("@/lib/api")>();
  return {
    ...actual,
    api: { ...actual.api, uploadDocumentWithProgress: vi.fn() },
  };
});

it("rejects an oversized file before starting its upload", async () => {
  render(
    <NextIntlClientProvider locale="zh-CN" messages={messages}>
      <UploadZone sourceId="source-1" maxMb={1} onUploaded={vi.fn()} />
    </NextIntlClientProvider>,
  );

  const file = new File(["x"], "too-large.pdf", { type: "application/pdf" });
  Object.defineProperty(file, "size", { value: 1024 * 1024 + 1 });
  fireEvent.change(screen.getByRole("button").querySelector("input")!, {
    target: { files: [file] },
  });

  await waitFor(() => {
    expect(vi.mocked(api.uploadDocumentWithProgress)).not.toHaveBeenCalled();
  });
});
