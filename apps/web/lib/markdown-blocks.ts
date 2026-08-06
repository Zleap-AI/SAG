/**
 * 把整份 Markdown 切成若干「块」，供分块渲染使用（大文件卡顿优化）。
 *
 * 目标：在顶层空行处断句，但绝不切断跨行结构——代码围栏（``` / ~~~）、
 * 表格连续行——否则各块独立解析会渲染错乱。小段落会合并到 ~TARGET 字符的
 * 目标窗口以控制块数；标题（ATX `#`）优先另起一块，保证章节边界自然。
 *
 * 不变式：blocks.join("") === 原文（不丢字、不改字），仅在边界插入分块点。
 */

// ~5000 字符/块：块变少减少 `content-visibility` 边界频繁触发的滚动跳动，
// 同时每块仍在浏览器单帧解析可承受的范围内。
const TARGET_CHARS = 5000;

const FENCE_RE = /^\s{0,3}(`{3,}|~{3,})/;
const HEADING_RE = /^\s{0,3}#{1,6}\s/;
const TABLE_ROW_RE = /^\s{0,3}\|/;

/** 一行是否开启/关闭一个代码围栏；返回围栏标记（``` 或 ~~~）或 null。 */
function fenceMarker(line: string): string | null {
  const match = FENCE_RE.exec(line);
  return match ? match[1][0] : null;
}

/**
 * 先把原文按「段落」切成原子单元：每个单元保留其后的换行。围栏与表格视为
 * 不可分割的整体。返回的单元拼接后完全等于原文。
 */
function toParagraphs(text: string): string[] {
  const lines = text.split("\n");
  const units: string[] = [];
  let current: string[] = [];
  let fence: string | null = null; // 当前打开的围栏标记（null 表示不在围栏内）

  const flush = () => {
    if (current.length) {
      // 每个元素已含各自换行（withNl），故以空串拼接还原原文。
      units.push(current.join(""));
      current = [];
    }
  };

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    const isLast = i === lines.length - 1;
    const withNl = isLast ? line : `${line}\n`;

    if (fence) {
      // 围栏内：整体累积，遇到同类闭合标记则关闭围栏。
      current.push(withNl);
      const marker = fenceMarker(line);
      if (marker && marker === fence) fence = null;
      continue;
    }

    const openMarker = fenceMarker(line);
    if (openMarker) {
      // 段落中途遇到围栏起始：先收尾已有段落，再进入围栏。
      flush();
      current.push(withNl);
      fence = openMarker;
      continue;
    }

    if (line.trim() === "") {
      // 空行是段落分隔：把空行并入当前单元末尾后收尾。
      current.push(withNl);
      flush();
      continue;
    }

    current.push(withNl);
  }
  flush();
  return units;
}

/** 单元是否以标题行起始（用于「标题优先另起块」）。 */
function startsWithHeading(unit: string): boolean {
  const firstLine = unit.split("\n", 1)[0] ?? "";
  return HEADING_RE.test(firstLine);
}

/** 单元是否是表格片段（含表格行）——避免与相邻单元错误合并造成断表。 */
function looksLikeTable(unit: string): boolean {
  return unit.split("\n").some((line) => TABLE_ROW_RE.test(line));
}

/**
 * 把整份 Markdown 切成块。空输入返回 []；否则至少返回一块。
 * 保证 result.join("") === text。
 */
export function splitMarkdownBlocks(text: string): string[] {
  if (!text) return [];
  const paragraphs = toParagraphs(text);
  if (paragraphs.length <= 1) return paragraphs;

  const blocks: string[] = [];
  let buffer = "";

  for (const unit of paragraphs) {
    const heading = startsWithHeading(unit);
    // 标题优先另起块：先把已累积的 buffer 收尾。
    if (heading && buffer) {
      blocks.push(buffer);
      buffer = "";
    }
    buffer += unit;
    // 达到目标窗口即收尾；表格片段不在中途切（looksLikeTable 的单元本身已完整）。
    if (buffer.length >= TARGET_CHARS && !looksLikeTable(unit)) {
      blocks.push(buffer);
      buffer = "";
    }
  }
  if (buffer) blocks.push(buffer);
  return blocks;
}
