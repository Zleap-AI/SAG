import { describe, expect, it } from "vitest";

import {
  buildFolderImportPlan,
  hasUnresolvedFolderImportConflicts,
  resolveFolderImportItem,
  setAllFolderImportItemsSelected,
  setFolderImportItemSelected,
  uploadableFolderImportItems,
} from "./folder-import";

function file(path: string, content: string, options?: { name?: string; size?: number }): File {
  const name = options?.name ?? path.split("/").at(-1) ?? "";
  const value = options?.size === undefined ? content : "x".repeat(options.size);
  const fixture = new File([value], name);
  Object.defineProperty(fixture, "webkitRelativePath", { value: path });
  return fixture;
}

describe("buildFolderImportPlan", () => {
  it("requires an explicit decision for existing and in-folder duplicate names", () => {
    const plan = buildFolderImportPlan(
      [file("notes/Plan.md", "a"), file("backup/plan.MD", "b"), file("new.txt", "c")],
      ["plan.md"],
      [".md", ".txt"],
      1024,
    );

    expect(plan.summary).toMatchObject({ eligible: 1, conflicts: 2, rejected: 0 });
    expect(plan.items[0]).toMatchObject({
      status: "conflict",
      duplicateWith: "both",
      decision: "undecided",
    });
    expect(plan.items[1]).toMatchObject({ status: "conflict", duplicateWith: "both" });
    expect(uploadableFolderImportItems(plan)).toHaveLength(1);
    expect(hasUnresolvedFolderImportConflicts(plan)).toBe(true);

    const decided = resolveFolderImportItem(plan, plan.items[0].id, "upload");
    expect(uploadableFolderImportItems(decided)).toHaveLength(1);
    expect(hasUnresolvedFolderImportConflicts(decided)).toBe(true);

    const fullyDecided = resolveFolderImportItem(decided, decided.items[1].id, "skip");
    expect(uploadableFolderImportItems(fullyDecided)).toHaveLength(2);
  });

  it("normalizes Unicode names and rejects unsupported, oversized, and nameless files", () => {
    const plan = buildFolderImportPlan(
      [
        file("Café.md", "a"),
        file("docs/Cafe\u0301.MD", "b"),
        file("bad.pdf", "c"),
        file("large.txt", "", { size: 11 }),
        file("ignored", "e", { name: "" }),
      ],
      [],
      [".md", ".txt"],
      10,
    );

    expect(plan.summary).toEqual({ eligible: 0, conflicts: 2, rejected: 3 });
    expect(plan.items.map((item) => [item.status, item.rejectReason, item.duplicateWith])).toEqual([
      ["conflict", undefined, "folder"],
      ["conflict", undefined, "folder"],
      ["rejected", "unsupported_type", null],
      ["rejected", "file_too_large", null],
      ["rejected", "missing_name", null],
    ]);
  });

  it("returns a new plan when resolving a matching item and leaves unknown items unchanged", () => {
    const plan = buildFolderImportPlan([file("note.md", "a")], ["note.md"], [".md"], 1024);

    expect(resolveFolderImportItem(plan, "missing", "skip")).toBe(plan);
    const resolved = resolveFolderImportItem(plan, plan.items[0].id, "skip");
    expect(resolved).not.toBe(plan);
    expect(resolved.items[0].decision).toBe("skip");
    expect(uploadableFolderImportItems(resolved)).toEqual([]);
  });

  it("selects every importable file by default and excludes a deselected duplicate from conflict review", () => {
    const plan = buildFolderImportPlan(
      [file("new.md", "new"), file("archive/old.md", "old")],
      ["old.md"],
      [".md"],
      1024,
    );
    const conflict = plan.items.find((item) => item.status === "conflict");

    expect(plan.items.map((item) => item.selected)).toEqual([true, true]);
    const deselected = setFolderImportItemSelected(plan, conflict!.id, false);
    expect(hasUnresolvedFolderImportConflicts(deselected)).toBe(false);
    expect(uploadableFolderImportItems(deselected).map((item) => item.name)).toEqual([
      "new.md",
    ]);
  });

  it("does not select rejected files when toggling all scanned items", () => {
    const plan = buildFolderImportPlan(
      [file("ok.md", "ok"), file("skip.pdf", "skip")],
      [],
      [".md"],
      1024,
    );

    const deselected = setAllFolderImportItemsSelected(plan, false);
    expect(deselected.items.map((item) => item.selected)).toEqual([false, false]);

    const selected = setAllFolderImportItemsSelected(deselected, true);
    expect(selected.items.map((item) => item.selected)).toEqual([true, false]);
  });
});
