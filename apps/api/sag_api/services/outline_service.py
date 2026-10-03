"""按 Markdown 标题层级组织文档大纲（PageIndex 式的章节树）。

引擎只为每个分块记录“最近的标题”文本，没有层级；这里从入库时保存的整篇
Markdown 中按阅读顺序读出 ``#``–``######`` 标题及其层级，再把分块挂到对应的
标题节点上。标题文本的规范化与引擎分块时一致（去 ``#``、链接只留文字、合并空白），
因此同名标题也能按出现顺序对上。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# 与引擎 Markdown 分块器的标题识别保持一致：行首 1–6 个 # 加空白。
_HEADING = re.compile(r"^(#{1,6})\s+(.+)$")
_FENCE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")
_LINK = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_UNTITLED = "（无标题分块）"


@dataclass(slots=True)
class OutlineNode:
    title: str
    level: int
    depth: int
    path: list[str]
    chunks: list[dict[str, Any]] = field(default_factory=list)


def normalize_heading(text: str) -> str:
    normalized = re.sub(r"^#{1,6}\s*", "", text.strip())
    normalized = _LINK.sub(r"\1", normalized)
    return " ".join(normalized.split())


def markdown_headings(markdown: str) -> list[tuple[int, str]]:
    """按阅读顺序返回 ``(level, title)``；围栏代码块中的 ``#`` 行不算标题。"""
    headings: list[tuple[int, str]] = []
    fence: str | None = None
    for line in markdown.splitlines():
        marker = _FENCE.match(line)
        if marker:
            token = marker.group(1)
            if fence is None:
                fence = token[0] * len(token)
            elif token[0] == fence[0] and len(token) >= len(fence):
                fence = None
            continue
        if fence is not None:
            continue
        match = _HEADING.match(line)
        if not match:
            continue
        title = normalize_heading(match.group(2))
        if title:
            headings.append((len(match.group(1)), title))
    return headings


def build_outline(markdown: str | None, rows: list[dict[str, Any]]) -> list[OutlineNode]:
    """把分块（按 rank 排序）挂到章节树上，返回按阅读顺序展开的节点列表。

    分块标题在 Markdown 中找不到时（例如来自非 Markdown 解析），归入一个层级为 0
    的独立节点，保证每个 chunk_id 都出现在大纲中。
    """
    headings = markdown_headings(markdown or "")
    nodes: list[OutlineNode] = []
    stack: list[OutlineNode] = []
    for level, title in headings:
        while stack and stack[-1].level >= level:
            stack.pop()
        node = OutlineNode(
            title=title,
            level=level,
            depth=len(stack),
            path=[*(parent.title for parent in stack), title],
        )
        nodes.append(node)
        stack.append(node)

    position = 0
    orphans: dict[str, OutlineNode] = {}
    ordered = list(nodes)
    for row in sorted(rows, key=lambda item: int(item.get("rank") or 0)):
        heading = normalize_heading(str(row.get("heading") or ""))
        target = _match(nodes, heading, position) if heading else None
        if target is not None:
            position = target
            nodes[target].chunks.append(row)
            continue
        key = heading or _UNTITLED
        orphan = orphans.get(key)
        if orphan is None:
            orphan = OutlineNode(title=key, level=0, depth=0, path=[key])
            orphans[key] = orphan
            ordered.append(orphan)
        orphan.chunks.append(row)
    return ordered


def _match(nodes: list[OutlineNode], heading: str, position: int) -> int | None:
    """优先从当前位置向后找（同名章节按出现顺序对应），找不到再回头找。"""
    for index in range(position, len(nodes)):
        if nodes[index].title == heading:
            return index
    for index in range(0, min(position, len(nodes))):
        if nodes[index].title == heading:
            return index
    return None


def render_outline_text(nodes: list[OutlineNode]) -> str:
    """MCP 文本：按层级缩进，每个章节后列出其分块序号与 chunk_id。"""
    lines: list[str] = []
    for node in nodes:
        indent = "  " * node.depth
        prefix = f"{'#' * node.level} " if node.level else ""
        line = f"{indent}{prefix}{node.title}"
        if node.chunks:
            refs = "、".join(
                f"{int(chunk.get('rank') or 0)}（chunk_id={chunk['chunk_id']}）"
                for chunk in node.chunks
            )
            line += f" — 分块 {refs}"
        lines.append(line)
    return "\n".join(lines)


def outline_rows(nodes: list[OutlineNode]) -> list[dict[str, Any]]:
    """REST 用的扁平行：保持按 rank 排序，并附带标题层级与章节路径。"""
    rows: list[dict[str, Any]] = []
    for node in nodes:
        for chunk in node.chunks:
            rows.append(
                {
                    "rank": int(chunk.get("rank") or 0),
                    "heading": chunk.get("heading") or "",
                    "chunk_id": chunk["chunk_id"],
                    "level": node.level or None,
                    "path": node.path if node.level else [],
                }
            )
    rows.sort(key=lambda row: row["rank"])
    return rows
