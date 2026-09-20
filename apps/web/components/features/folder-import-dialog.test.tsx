/** @vitest-environment jsdom */

import * as React from "react";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { renderToStaticMarkup } from "react-dom/server";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { TooltipProvider } from "@/components/ui/tooltip";
import { buildFolderImportPlan } from "@/lib/folder-import";
import messages from "@/messages/en-US.json";
import {
  FolderImportDialog,
  FolderImportSelectionList,
} from "./folder-import-dialog";

vi.mock("@/lib/diagnostics", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/diagnostics")>();
  return {
    ...actual,
    getDiagnosticsStore: () => ({ record: vi.fn() }),
  };
});

beforeEach(() => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean })
    .IS_REACT_ACT_ENVIRONMENT = true;
});

afterEach(() => {
  document.body.replaceChildren();
  vi.restoreAllMocks();
});

async function renderSelectionStep() {
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);

  await act(async () => {
    root.render(
      <NextIntlClientProvider
        locale="en-US"
        timeZone="UTC"
        messages={messages}
      >
        <TooltipProvider>
          <FolderImportDialog
            sourceId="source-1"
            existingDocumentNames={[]}
            allowedExts={[".md"]}
            maxMb={25}
            onFinished={vi.fn()}
            onClose={vi.fn()}
          />
        </TooltipProvider>
      </NextIntlClientProvider>,
    );
  });

  const fileInput = container.querySelector('input[type="file"]');
  if (!(fileInput instanceof HTMLInputElement)) {
    throw new Error("folder import file input not found");
  }
  Object.defineProperty(fileInput, "files", {
    configurable: true,
    value: Array.from(
      { length: 12 },
      (_, index) => new File([`content-${index}`], `document-${index}.md`),
    ),
  });
  await act(async () => {
    fileInput.dispatchEvent(new Event("change", { bubbles: true }));
  });

  const selectFiles = Array.from(container.querySelectorAll("button")).find(
    (button) => button.textContent?.trim() === "Select files",
  );
  if (!(selectFiles instanceof HTMLButtonElement)) {
    throw new Error("select files action not found");
  }
  await act(async () => selectFiles.click());

  return { container, root };
}

describe("FolderImportDialog", () => {
  it("keeps accessible file and folder choices visible before scanning", () => {
    const html = renderToStaticMarkup(
      <NextIntlClientProvider
        locale="en-US"
        timeZone="UTC"
        messages={messages}
      >
        <TooltipProvider>
          <FolderImportDialog
            sourceId="source-1"
            existingDocumentNames={[]}
            allowedExts={[".md", ".txt"]}
            maxMb={25}
            onFinished={vi.fn()}
            onClose={vi.fn()}
          />
        </TooltipProvider>
      </NextIntlClientProvider>,
    );

    expect(html).toContain('type="file"');
    expect(html).toContain("multiple");
    expect(html).toContain("webkitdirectory");
    expect(html).toContain('aria-live="polite"');
    expect(html).toContain("Choose folder");
    expect(html).toContain("Scan result");
    expect(html).toContain("Inspect conflicts");
    expect(html).toContain("Final confirmation");
  });

  it("renders checked selectable candidates and leaves rejected files disabled", () => {
    const plan = buildFolderImportPlan(
      [new File(["ok"], "ok.md"), new File(["skip"], "skip.pdf")],
      [],
      [".md"],
      1024,
    );
    const html = renderToStaticMarkup(
      <NextIntlClientProvider
        locale="en-US"
        timeZone="UTC"
        messages={messages}
      >
        <FolderImportSelectionList
          plan={plan}
          onSelectAll={vi.fn()}
          onSelectItem={vi.fn()}
        />
      </NextIntlClientProvider>,
    );

    expect(messages.FolderImport.selectFiles).toBe("Select files");
    expect(html).toContain("Select all importable files");
    expect(html).toContain('checked=""');
    expect(html).toContain("disabled");
  });

  it("keeps long file names inside the dialog width", () => {
    const longName = `${"long-document-name-".repeat(20)}.pdf`;
    const plan = buildFolderImportPlan(
      [new File(["content"], longName)],
      [],
      [".pdf"],
      1024,
    );
    const html = renderToStaticMarkup(
      <NextIntlClientProvider
        locale="en-US"
        timeZone="UTC"
        messages={messages}
      >
        <FolderImportSelectionList
          plan={plan}
          onSelectAll={vi.fn()}
          onSelectItem={vi.fn()}
        />
      </NextIntlClientProvider>,
    );

    expect(html).toContain(
      'class="flex min-h-0 min-w-0 flex-1 flex-col gap-3 overflow-hidden"',
    );
    expect(html).toContain(
      'class="min-h-0 min-w-0 flex-1 space-y-2 overflow-y-auto pr-1"',
    );
    expect(html).toContain('class="min-w-0 rounded-md border p-3"');
  });

  it("keeps selection actions visible while the file list scrolls", async () => {
    const { container, root } = await renderSelectionStep();
    const dialogSection = container.querySelector(
      'section[aria-labelledby="folder-import-title"]',
    );
    const fileList = container.querySelector("ul[aria-label]");
    const continueButton = Array.from(container.querySelectorAll("button")).find(
      (button) => button.textContent?.trim() === "Continue",
    );

    expect(dialogSection?.className).toContain("min-h-0");
    expect(dialogSection?.className).toContain("overflow-hidden");
    expect(fileList?.className).toContain("flex-1");
    expect(fileList?.className).toContain("overflow-y-auto");
    expect(continueButton?.parentElement?.className).toContain("shrink-0");

    await act(async () => root.unmount());
  });
});
