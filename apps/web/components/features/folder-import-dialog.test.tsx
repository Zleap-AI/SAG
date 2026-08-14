import * as React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { NextIntlClientProvider } from "next-intl";
import { describe, expect, it, vi } from "vitest";

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
});
