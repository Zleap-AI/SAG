import { describe, expect, it } from "vitest";

import { splitMarkdownBlocks } from "./markdown-blocks";

/** 所有用例共有的核心不变式：分块拼接后完全等于原文。 */
function expectLossless(text: string) {
  expect(splitMarkdownBlocks(text).join("")).toBe(text);
}

describe("splitMarkdownBlocks", () => {
  it("returns [] for empty input", () => {
    expect(splitMarkdownBlocks("")).toEqual([]);
  });

  it("keeps a single short paragraph as one block", () => {
    const text = "just one paragraph";
    expect(splitMarkdownBlocks(text)).toEqual([text]);
  });

  it("splits paragraphs at blank lines when large enough", () => {
    const para = "x".repeat(3000);
    const text = `${para}\n\n${para}\n\n${para}`;
    const blocks = splitMarkdownBlocks(text);
    expect(blocks.length).toBeGreaterThan(1);
    expectLossless(text);
  });

  it("never splits inside a fenced code block", () => {
    const bigCode = Array.from({ length: 200 }, (_, i) => `line ${i}`).join("\n");
    const text = `intro\n\n\`\`\`js\n${bigCode}\n\`\`\`\n\noutro`;
    const blocks = splitMarkdownBlocks(text);
    // 围栏必须完整落在某一个块里（开合成对出现在同一块）。
    const fenceBlock = blocks.find((b) => b.includes("```js"));
    expect(fenceBlock).toBeDefined();
    expect((fenceBlock!.match(/```/g) ?? []).length).toBe(2);
    expectLossless(text);
  });

  it("treats ~~~ fences the same way", () => {
    const bigCode = "row\n".repeat(500);
    const text = `a\n\n~~~\n${bigCode}~~~\n\nb`;
    const fenceBlock = splitMarkdownBlocks(text).find((b) => b.includes("~~~"));
    expect(fenceBlock).toBeDefined();
    expect((fenceBlock!.match(/~~~/g) ?? []).length).toBe(2);
    expectLossless(text);
  });

  it("does not split a large table across blocks", () => {
    const header = "| a | b |\n| --- | --- |\n";
    const rows = Array.from({ length: 400 }, (_, i) => `| ${i} | v${i} |`).join("\n");
    const text = `# Title\n\n${header}${rows}\n\ntail`;
    const blocks = splitMarkdownBlocks(text);
    const tableBlock = blocks.find((b) => b.includes("| --- |"));
    expect(tableBlock).toBeDefined();
    // 所有表格行都在同一个块内（不被 TARGET 窗口从中切断）。
    expect((tableBlock!.match(/^\s{0,3}\|/gm) ?? []).length).toBe(402);
    expectLossless(text);
  });

  it("starts a new block at a heading", () => {
    const filler = "y".repeat(300);
    const text = `${filler}\n\n# Heading\n\nafter`;
    const blocks = splitMarkdownBlocks(text);
    const headingBlock = blocks.find((b) => b.trimStart().startsWith("# Heading"));
    expect(headingBlock).toBeDefined();
    // 标题前的内容不与标题挤在同一块。
    expect(headingBlock!.startsWith(filler)).toBe(false);
    expectLossless(text);
  });

  it("produces a reasonable block count for very large input", () => {
    const para = "z".repeat(1800);
    const text = Array.from({ length: 100 }, () => para).join("\n\n");
    const blocks = splitMarkdownBlocks(text);
    expect(blocks.length).toBeGreaterThan(10);
    expect(blocks.length).toBeLessThan(text.length); // 明显是分块而非逐字
    expectLossless(text);
  });
});
