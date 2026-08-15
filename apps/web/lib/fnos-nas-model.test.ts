import { describe, expect, it } from "vitest";

import type { FnOSNasScanResult } from "./types";
import {
  initialFnOSNasImportState,
  reduceFnOSNasImport,
  selectEligibleFiles,
  selectFilteredFiles,
  selectImportTotals,
  selectPageFiles,
  selectSelectedTokens,
} from "./fnos-nas-model";

const scan: FnOSNasScanResult = {
  scan_id: "scan-1",
  folder: "Team documents",
  truncated: false,
  truncated_reason: null,
  selection_expires_at: "2026-08-13T12:00:00Z",
  summary: {
    visited: 4,
    eligible: 2,
    new: 1,
    changed: 1,
    imported: 1,
    unsupported: 1,
    too_large: 0,
    unreadable: 0,
  },
  files: [
    {
      selection_token: "new-token",
      name: "handbook.pdf",
      display_path: "Policies/handbook.pdf",
      extension: ".pdf",
      size_bytes: 1024,
      modified_at: "2026-08-13T10:00:00Z",
      state: "new",
      selected_by_default: true,
      document_id: null,
    },
    {
      selection_token: "changed-token",
      name: "guide.md",
      display_path: "Policies/guide.md",
      extension: ".md",
      size_bytes: 3072,
      modified_at: "2026-08-13T09:00:00Z",
      state: "changed",
      selected_by_default: true,
      document_id: "doc-guide",
    },
    {
      selection_token: null,
      name: "old.pdf",
      display_path: "Archive/old.pdf",
      extension: ".pdf",
      size_bytes: 100,
      modified_at: "2026-08-12T09:00:00Z",
      state: "imported",
      selected_by_default: false,
      document_id: "doc-old",
    },
    {
      selection_token: null,
      name: "video.mp4",
      display_path: "Media/video.mp4",
      extension: ".mp4",
      size_bytes: 100,
      modified_at: "2026-08-11T09:00:00Z",
      state: "unsupported",
      selected_by_default: false,
      document_id: null,
    },
  ],
};

describe("fnOS NAS import model", () => {
  it("selects only new and changed files by default", () => {
    const state = reduceFnOSNasImport(initialFnOSNasImportState, {
      type: "scan.loaded",
      result: scan,
    });

    expect(selectSelectedTokens(state)).toEqual(["new-token", "changed-token"]);
    expect(selectImportTotals(state)).toEqual({ files: 2, bytes: 4096 });
    expect(selectEligibleFiles(state).map((file) => file.name)).toEqual([
      "handbook.pdf",
      "guide.md",
    ]);
  });

  it("filters deterministically and resets pagination", () => {
    let state = reduceFnOSNasImport(initialFnOSNasImportState, {
      type: "scan.loaded",
      result: scan,
    });
    state = reduceFnOSNasImport(state, { type: "page.changed", page: 2 });
    state = reduceFnOSNasImport(state, {
      type: "filter.changed",
      filter: "query",
      value: "policies",
    });
    expect(state.page).toBe(1);
    expect(selectFilteredFiles(state).map((file) => file.name)).toEqual([
      "handbook.pdf",
      "guide.md",
    ]);
    expect(selectPageFiles({ ...state, pageSize: 1 }).map((file) => file.name)).toEqual([
      "handbook.pdf",
    ]);
  });

  it("preserves the last successful result while a rescan is in flight", () => {
    let state = reduceFnOSNasImport(initialFnOSNasImportState, {
      type: "scan.loaded",
      result: scan,
    });
    state = reduceFnOSNasImport(state, { type: "selection.cleared" });
    state = reduceFnOSNasImport(state, {
      type: "selection.page",
      selected: true,
    });
    expect(selectSelectedTokens(state)).toEqual(["new-token", "changed-token"]);
    state = reduceFnOSNasImport(state, {
      type: "import.progress",
      progress: {
        id: "job-1",
        status: "succeeded",
        progress: 1,
        total: 2,
        completed: 2,
        created: 1,
        updated: 0,
        skipped: 0,
        failed: 1,
        results: [],
      },
    });
    expect(state.stage).toBe("complete");
    state = reduceFnOSNasImport(state, { type: "scan.started" });
    expect(state.stage).toBe("scanning");
    expect(state.scan).toBe(scan);
    expect(selectSelectedTokens(state)).toEqual(["new-token", "changed-token"]);
    expect(state.importProgress).toBeNull();

    state = reduceFnOSNasImport(state, { type: "scan.cancelled" });
    expect(state.stage).toBe("loaded");
    expect(state.scan).toBe(scan);
    expect(selectSelectedTokens(state)).toEqual(["new-token", "changed-token"]);

    state = reduceFnOSNasImport(state, { type: "scan.started" });
    state = reduceFnOSNasImport(state, { type: "scan.failed", message: "offline" });
    expect(state.stage).toBe("error");
    expect(state.scan).toBe(scan);
    expect(state.error).toBe("offline");
  });
});
