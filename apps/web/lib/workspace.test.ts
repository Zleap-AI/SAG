import { describe, expect, it } from "vitest";

import {
  WORKSPACE_SECTIONS,
  isWorkspaceSection,
  workspaceSectionDefinition,
  workspaceSectionFromPathname,
} from "./workspace";

describe("workspace sections", () => {
  it("keeps normal and compact navigation on one ordered definition", () => {
    expect(WORKSPACE_SECTIONS.map((item) => item.id)).toEqual([
      "search",
      "answer",
      "knowledge",
    ]);
  });

  it.each([
    ["/search", "search"],
    ["/search/results", "search"],
    ["/chat", "answer"],
    ["/chat/thread-1", "answer"],
    ["/knowledge", "knowledge"],
    ["/knowledge/source-1", "knowledge"],
    ["/settings", null],
  ])("maps %s to %s", (pathname, expected) => {
    expect(workspaceSectionFromPathname(pathname)).toBe(expected);
  });

  it("validates persisted values and resolves section metadata", () => {
    expect(isWorkspaceSection("knowledge")).toBe(true);
    expect(isWorkspaceSection("explore")).toBe(false);
    expect(workspaceSectionDefinition("answer")).toMatchObject({
      id: "answer",
      href: "/chat",
    });
  });
});
