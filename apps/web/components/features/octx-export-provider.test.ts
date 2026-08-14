import { describe, expect, it } from "vitest";

import { projectDesktopDiagnostics } from "./octx-export-provider";

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
