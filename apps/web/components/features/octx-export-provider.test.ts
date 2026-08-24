import { describe, expect, it } from "vitest";

import type { PersistedOctxExportTask } from "@/lib/octx-export-tasks";

import { artifactFilename, projectDesktopDiagnostics } from "./octx-export-provider";

describe("artifactFilename", () => {
  it("preserves the knowledge base name and uses the OCTX suffix", () => {
    const task = {
      transferId: "transfer-1",
      sourceId: "source-1",
      sourceName: "AI 手册",
      filenameHint: "AI 手册",
      autoDownloaded: false,
      createdAt: "2026-08-24T00:00:00.000Z",
      transfer: {
        id: "transfer-1",
        direction: "export",
        status: "ready",
        progress: 1,
        asset: null,
        release: { id: "release-1", version: "1.0.0", package_digest: "sha256:test" },
        target_source_id: "source-1",
        installation_id: null,
        allowed_actions: [],
        decision_token: null,
        conflicts: [],
        excluded_documents: [],
        record_counts: {},
        capabilities: {},
        validation_report: null,
        warnings: [],
        error: null,
        cancellation_requested: false,
        created_at: "2026-08-24T00:00:00.000Z",
        updated_at: "2026-08-24T00:00:00.000Z",
      },
    } satisfies PersistedOctxExportTask;

    expect(artifactFilename(task)).toBe("AI 手册-OCTX.octx");
  });
});

describe("projectDesktopDiagnostics", () => {
  it("omits raw desktop log data from OCTX diagnostics downloads", () => {
    const diagnostics = projectDesktopDiagnostics({
      version: "1.2.3",
      platform: "darwin",
      arch: "arm64",
      osRelease: "24.0.0",
      osVersion: "macOS",
      packaged: true,
      electron: "37.0.0",
      chrome: "138.0.0",
      node: "22.0.0",
      logFiles: [
        {
          name: "document-content.log",
          path: "/Users/alice/SAG/logs/main.log",
          sizeBytes: 512,
          truncated: true,
          content:
            "Authorization: Basic YWxpY2U6c2VjcmV0\nCookie: sag_token=secret\npostgresql://alice:secret@db.example/sag\nDocument content payload\nfree-form secret",
        },
      ],
    });

    expect(diagnostics).toEqual({
      version: "1.2.3",
      platform: "darwin",
      arch: "arm64",
      os_release: "24.0.0",
      os_version: "macOS",
      packaged: true,
      electron: "37.0.0",
      chrome: "138.0.0",
      node: "22.0.0",
      log_file_count: 1,
      has_truncated_logs: true,
    });

    const serialized = JSON.stringify(diagnostics);
    for (const unsafeValue of [
      "Authorization: Basic",
      "Cookie:",
      "postgresql://",
      "Document content payload",
      "free-form secret",
      "document-content.log",
      "/Users/alice",
    ]) {
      expect(serialized).not.toContain(unsafeValue);
    }
  });
});
