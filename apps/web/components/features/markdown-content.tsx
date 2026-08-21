"use client";

import * as React from "react";
import { useTranslations } from "next-intl";
import ReactMarkdown from "react-markdown";
import rehypeKatex from "rehype-katex";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";

import { splitMarkdownBlocks } from "@/lib/markdown-blocks";
import type { Citation } from "@/lib/types";
import { cn } from "@/lib/utils";

type MdNode = {
  type?: string;
  value?: string;
  url?: string;
  title?: string | null;
  children?: MdNode[];
  data?: Record<string, unknown>;
};

type HastNode = {
  type?: string;
  value?: string;
  properties?: { className?: unknown };
  children?: HastNode[];
};

/** Convert paired LaTeX delimiters only; never infer math from its contents. */
function normalizeExplicitMathDelimiters(content: string): string {
  let normalized = "";
  let codeFence: string | null = null;
  let inlineCodeTicks = 0;
  let linkDestinationDepth = 0;
  let autolinkDestination = false;

  const hasOddBackslashRun = (index: number) => {
    let count = 0;
    for (let cursor = index; cursor >= 0 && content[cursor] === "\\"; cursor--) count++;
    return count % 2 === 1;
  };

  const isEscaped = (index: number) => {
    let count = 0;
    for (let cursor = index - 1; cursor >= 0 && content[cursor] === "\\"; cursor--) count++;
    return count % 2 === 1;
  };

  const findExplicitClose = (
    start: number,
    token: "\\)" | "\\]",
    inline: boolean,
  ) => {
    for (let cursor = start; cursor < content.length - 1; cursor++) {
      if (inline && content[cursor] === "\n") return -1;
      if (content[cursor] === "`") return -1;
      const lineStart = cursor === 0 || content[cursor - 1] === "\n";
      if (lineStart && /^ {0,3}(?:`{3,}|~{3,})/.test(content.slice(cursor))) return -1;
      if (content.startsWith(token, cursor) && hasOddBackslashRun(cursor)) return cursor;
    }
    return -1;
  };

  const findNextDollar = (start: number) => {
    for (let cursor = start; cursor < content.length; cursor++) {
      if (content[cursor] === "$" && !isEscaped(cursor)) return cursor;
    }
    return -1;
  };

  const startsLiteralDollarToken = (value: string) => {
    return /^(?:\s*\d|[A-Z][A-Z0-9_]*(?=$|[^A-Z0-9_]))/.test(value);
  };

  for (let index = 0; index < content.length;) {
    const lineStart = index === 0 || content[index - 1] === "\n";
    if (lineStart && inlineCodeTicks === 0) {
      const fenceMatch = /^( {0,3})(`{3,}|~{3,})/.exec(content.slice(index));
      if (fenceMatch) {
        const marker = fenceMatch[2];
        const newlineIndex = content.indexOf("\n", index);
        const lineEnd = newlineIndex === -1 ? content.length : newlineIndex + 1;
        const markerSuffix = content.slice(index + fenceMatch[0].length, lineEnd).trim();
        if (!codeFence) codeFence = marker;
        else if (
          codeFence[0] === marker[0]
          && marker.length >= codeFence.length
          && markerSuffix === ""
        ) codeFence = null;
        normalized += content.slice(index, lineEnd);
        index = lineEnd;
        continue;
      }

      if (!codeFence && /^ {0,3}\[[^\]\n]+\]:\s*/.test(content.slice(index))) {
        const newlineIndex = content.indexOf("\n", index);
        const lineEnd = newlineIndex === -1 ? content.length : newlineIndex + 1;
        normalized += content.slice(index, lineEnd);
        index = lineEnd;
        continue;
      }
    }

    if (!codeFence && content[index] === "`") {
      let runLength = 1;
      while (content[index + runLength] === "`") runLength++;
      if (inlineCodeTicks === 0) inlineCodeTicks = runLength;
      else if (inlineCodeTicks === runLength) inlineCodeTicks = 0;
      normalized += content.slice(index, index + runLength);
      index += runLength;
      continue;
    }

    if (!codeFence && inlineCodeTicks === 0) {
      if (autolinkDestination) {
        if (content[index] === ">" && !isEscaped(index)) autolinkDestination = false;
        normalized += content[index];
        index++;
        continue;
      }
      if (
        content[index] === "<"
        && /^(?:[A-Za-z][A-Za-z0-9+.-]{1,31}:|[^ <>@]+@)/.test(content.slice(index + 1))
      ) {
        autolinkDestination = true;
        normalized += content[index];
        index++;
        continue;
      }
      if (linkDestinationDepth > 0) {
        if (content[index] === "(" && !isEscaped(index)) linkDestinationDepth++;
        else if (content[index] === ")" && !isEscaped(index)) linkDestinationDepth--;
        normalized += content[index];
        index++;
        continue;
      }
      if (content[index] === "(" && content[index - 1] === "]" && !isEscaped(index)) {
        linkDestinationDepth = 1;
        normalized += content[index];
        index++;
        continue;
      }
    }

    if (
      !codeFence
      && inlineCodeTicks === 0
      && content.startsWith("\\(", index)
      && hasOddBackslashRun(index)
    ) {
      const closingIndex = findExplicitClose(index + 2, "\\)", true);
      if (closingIndex !== -1) {
        normalized += `$${content.slice(index + 2, closingIndex)}$`;
        index = closingIndex + 2;
        continue;
      }
      normalized += "\\\\(";
      index += 2;
      continue;
    }

    if (
      !codeFence
      && inlineCodeTicks === 0
      && content.startsWith("\\[", index)
      && hasOddBackslashRun(index)
    ) {
      const closingIndex = findExplicitClose(index + 2, "\\]", false);
      if (closingIndex !== -1) {
        const math = content.slice(index + 2, closingIndex).replace(/^\s+|\s+$/g, "");
        normalized += `\n$$\n${math}\n$$\n`;
        index = closingIndex + 2;
        continue;
      }
      normalized += "\\\\[";
      index += 2;
      continue;
    }

    if (
      !codeFence
      && inlineCodeTicks === 0
      && content[index] === "$"
      && !isEscaped(index)
      && startsLiteralDollarToken(content.slice(index + 1))
    ) {
      const closingIndex = findNextDollar(index + 1);
      if (
        closingIndex === -1
        || startsLiteralDollarToken(content.slice(closingIndex + 1))
      ) {
        normalized += "\\$";
        index++;
        continue;
      }
    }

    if (
      !codeFence
      && inlineCodeTicks === 0
      && (content.startsWith("\\)", index) || content.startsWith("\\]", index))
      && hasOddBackslashRun(index)
    ) {
      normalized += `\\\\${content[index + 1]}`;
      index += 2;
      continue;
    }

    normalized += content[index];
    index++;
  }
  return normalized;
}

function remarkCitationLinks(validNumbers: ReadonlySet<string>) {
  return () => {
    const visit = (node: MdNode) => {
      if (node.type === "link" || node.type === "code" || node.type === "inlineCode") return;
      if (!node.children) return;
      node.children = node.children.flatMap((child) => {
        if (child.type !== "text" || typeof child.value !== "string") {
          visit(child);
          return [child];
        }

        const parts: MdNode[] = [];
        const re = /\[(\d+)\]/g;
        let last = 0;
        let match: RegExpExecArray | null;
        while ((match = re.exec(child.value))) {
          // A bracketed number is only interactive when the backend supplied
          // traceable metadata for that exact number. Never manufacture a
          // disabled "citation" control for model-invented references.
          if (!validNumbers.has(match[1])) continue;
          if (match.index > last) {
            parts.push({ type: "text", value: child.value.slice(last, match.index) });
          }
          parts.push({
            type: "link",
            // Hash URLs survive react-markdown's URL sanitizer; the renderer below
            // replaces them with buttons, so citation clicks never navigate.
            url: `#citation-${match[1]}`,
            title: null,
            children: [{ type: "text", value: match[1] }],
            data: { hProperties: { "data-citation": match[1] } },
          });
          last = match.index + match[0].length;
        }
        if (!parts.length) return [child];
        if (last < child.value.length) {
          parts.push({ type: "text", value: child.value.slice(last) });
        }
        return parts;
      });
    };
    return visit;
  };
}

/**
 * 知识库与模型输出常把百分号写成 `5.2%`，而 TeX 会把裸 `%` 当作注释起点。
 * 在 rehype-katex 消费数学 HAST 前补全转义，避免改写普通 Markdown、URL 或代码块。
 */
function rehypeMathLiteralPercent() {
  return (tree: HastNode) => {
    const escapeLiteralPercents = (value: string) => {
      let escaped = "";
      for (let index = 0; index < value.length; index++) {
        const character = value[index];
        if (character !== "%") {
          escaped += character;
          continue;
        }

        let precedingBackslashes = 0;
        for (let cursor = index - 1; cursor >= 0 && value[cursor] === "\\"; cursor--) {
          precedingBackslashes++;
        }
        escaped += precedingBackslashes % 2 === 0 ? "\\%" : "%";
      }
      return escaped;
    };

    const escapeText = (node: HastNode) => {
      if (node.type === "text" && typeof node.value === "string") {
        node.value = escapeLiteralPercents(node.value);
      }
      node.children?.forEach(escapeText);
    };
    const visit = (node: HastNode) => {
      const rawClassName = node.properties?.className;
      const classNames = Array.isArray(rawClassName)
        ? rawClassName.filter((value): value is string => typeof value === "string")
        : typeof rawClassName === "string"
          ? rawClassName.split(/\s+/)
          : [];
      if (classNames.some((name) => name === "math-inline" || name === "math-display")) {
        node.children?.forEach(escapeText);
        return;
      }
      node.children?.forEach(visit);
    };
    visit(tree);
  };
}

function MdImage(props: React.ImgHTMLAttributes<HTMLImageElement>) {
  const t = useTranslations("Markdown");
  const [broken, setBroken] = React.useState(false);
  const src = typeof props.src === "string" ? props.src : "";
  const external = /^(https?:|data:|blob:)/.test(src);
  if (broken || !external) {
    return (
      <span className="my-1 inline-flex max-w-full items-center gap-1.5 rounded-md border border-dashed bg-muted/40 px-2 py-1 text-xs text-muted-foreground">
        {t("imageUnavailable", { alt: props.alt ?? "" })}
      </span>
    );
  }
  // eslint-disable-next-line @next/next/no-img-element
  return (
    <img
      {...props}
      alt={props.alt ?? t("image")}
      onError={() => setBroken(true)}
      className="my-2 max-h-80 max-w-full rounded-md border"
    />
  );
}

export const MarkdownContent = React.memo(function MarkdownContent({
  content,
  citations,
  onCitationClick,
  streaming = false,
}: {
  content: string;
  citations?: Citation[];
  onCitationClick?: (citation: Citation) => void;
  streaming?: boolean;
}) {
  const t = useTranslations("Markdown");
  const citationByNumber = React.useMemo(() => {
    return new Map(
      (citations ?? [])
        .filter(
          (citation) => citation.kind !== "external"
            && Number.isInteger(citation.n)
            && citation.n > 0
            && Boolean(citation.chunk_id && citation.source_id),
        )
        .map((citation) => [String(citation.n), citation]),
    );
  }, [citations]);
  const citationPlugin = React.useMemo(
    () => remarkCitationLinks(new Set(citationByNumber.keys())),
    [citationByNumber],
  );

  return (
    <div
      className={cn("answer-prose text-foreground", streaming && "answer-prose--streaming")}
      aria-busy={streaming || undefined}
    >
      <ReactMarkdown
        remarkPlugins={[remarkGfm, remarkMath, citationPlugin]}
        rehypePlugins={[
          rehypeMathLiteralPercent,
          [
            rehypeKatex,
            {
              trust: false,
              throwOnError: false,
              strict: "warn",
              maxSize: 100,
              maxExpand: 1000,
            },
          ],
        ]}
        components={{
          img: MdImage,
          a: ({ href, children, ...props }) => {
            const citationMatch = href?.match(/^#citation-(\d+)$/);
            if (citationMatch) {
              const n = citationMatch[1];
              const citation = citationByNumber.get(n);
              return (
                <button
                  type="button"
                  disabled={!citation}
                  onClick={(event) => {
                    event.preventDefault();
                    event.stopPropagation();
                    if (citation) onCitationClick?.(citation);
                  }}
                  className={cn(
                    "relative -top-px mx-0.5 inline-flex size-[18px] items-center justify-center rounded-full bg-muted font-mono text-[10px] font-semibold leading-none text-muted-foreground no-underline outline-none transition-colors align-baseline focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-1",
                    citation
                      ? "cursor-pointer hover:bg-muted-foreground/20 hover:text-foreground"
                      : "cursor-default opacity-60",
                  )}
                  aria-label={citation ? t("openSource", { number: n }) : t("source", { number: n })}
                  title={citation?.heading || t("source", { number: n })}
                >
                  {children}
                </button>
              );
            }
            return (
              <a href={href} target="_blank" rel="noopener noreferrer" {...props}>
                {children}
              </a>
            );
          },
        }}
      >
        {normalizeExplicitMathDelimiters(content)}
      </ReactMarkdown>
    </div>
  );
});

/**
 * 大文档分块渲染：把整份内容切成若干块，每块独立 `MarkdownContent`，块容器加
 * `content-visibility:auto`（见 globals.css `.md-block`），让浏览器跳过屏幕外块的
 * 布局/绘制。解析与 DOM 都摊成块，避免一次性渲染超大文档导致的卡顿/冻结。
 *
 * 仅用于「解析内容」这类无引用（citations）的长文本；答案流仍走 `MarkdownContent`。
 * 内容较短时（单块）直接渲染，不引入额外包裹。
 */
export const ChunkedMarkdown = React.memo(function ChunkedMarkdown({
  content,
}: {
  content: string;
}) {
  const blocks = React.useMemo(() => splitMarkdownBlocks(content), [content]);
  if (blocks.length <= 1) {
    return <MarkdownContent content={content} />;
  }
  return (
    <>
      {blocks.map((block, index) => (
        // 索引作 key 安全：blocks 由 content 纯函数派生，同一 content 顺序稳定。
        <div key={index} className="md-block">
          <MarkdownContent content={block} />
        </div>
      ))}
    </>
  );
});

/**
 * 大文档原始文本分块渲染：与 `ChunkedMarkdown` 同一套切块 + `.md-block` 策略，
 * 但不走 Markdown 解析，块内为纯文本 `<pre>`。用于详情面板「原始 Markdown」
 * 模式，避免单块超长 `pre-wrap` 在面板拖宽时全量换行重排。
 *
 * 短文本（单块）直接渲染，不引入额外包裹；答案流 / 预览模式不受影响。
 */
export const ChunkedRawText = React.memo(function ChunkedRawText({
  content,
}: {
  content: string;
}) {
  const blocks = React.useMemo(() => splitMarkdownBlocks(content), [content]);
  // 与改前 TextBody raw 的 `<pre>` 字号/换行一致；m-0 避免多块相邻时浏览器默认 pre 外边距叠出缝隙。
  const textClassName =
    "m-0 whitespace-pre-wrap break-words font-mono text-xs leading-relaxed";

  if (blocks.length <= 1) {
    return <pre className={textClassName}>{content}</pre>;
  }
  return (
    <>
      {blocks.map((block, index) => (
        // 索引作 key 安全：blocks 由 content 纯函数派生，同一 content 顺序稳定。
        <div key={index} className="md-block">
          <pre className={textClassName}>{block}</pre>
        </div>
      ))}
    </>
  );
});
