"""把 SAG 知识库的检索、实体与原文能力暴露为标准 MCP server。

一个 SAG 实例只构造一个 FastMCP server。每次调用可作用于全部信源，也可以通过
``source_id`` 收窄到单个信源：HTTP 包装层、进程内 Agent 和 stdio 入口都通过
``MCPScope`` 注入当前可见信源，工具本身不依赖传输方式。
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, TypedDict

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import Field

from sag_api.services.retrieval_service import retrieve_relevant_sections

if TYPE_CHECKING:
    from sag_api.db.models import Document, Source
    from sag_api.sag import EngineManager


class MCPToolDetail(TypedDict):
    name: str
    label: str
    description: str


MCP_TOOL_DETAILS: tuple[MCPToolDetail, ...] = (
    {
        "name": "list_sources",
        "label": "查看信源",
        "description": "查看当前可访问的知识来源、文档数和分块数，并获取 source_id。",
    },
    {
        "name": "search",
        "label": "语义检索",
        "description": "按含义查找相关资料，适合自然语言问题、概念和模糊表述；返回证据片段及 chunk_id。",
    },
    {
        "name": "get_entity",
        "label": "查询实体",
        "description": "查找人物、组织或概念，并汇总它在资料中的相关上下文。",
    },
    {
        "name": "list_documents",
        "label": "查看文档",
        "description": "列出文档、处理状态和分块数量，并获取 document_id。",
    },
    {
        "name": "outline",
        "label": "文档大纲",
        "description": "查看指定文档的章节和分块结构，并获取 chunk_id，便于快速定位内容。",
    },
    {
        "name": "grep",
        "label": "精确查找",
        "description": "按原文字面内容查找，适合专名、编号、固定短语和代码；返回命中上下文及 chunk_id。",
    },
    {
        "name": "read",
        "label": "按行读原文",
        "description": "按行分页读取指定文档的原始文本，适合查看连续上下文。",
    },
    {
        "name": "get_chunk",
        "label": "读取分块",
        "description": "通过 chunk_id 读取一个分块的完整原文，用于核对和引用证据。",
    },
)
MCP_TOOL_NAMES = tuple(tool["name"] for tool in MCP_TOOL_DETAILS)
MCP_TOOL_LABELS = {tool["name"]: tool["label"] for tool in MCP_TOOL_DETAILS}
MCP_TOOL_DESCRIPTIONS = {tool["name"]: tool["description"] for tool in MCP_TOOL_DETAILS}
READ_ONLY_TOOL_ANNOTATIONS = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)

SourceId = Annotated[
    str,
    Field(description="可选。来自 list_sources；留空时查询全部可见信源。"),
]
DocumentId = Annotated[str, Field(description="来自 list_documents 的文档 ID。")]
ChunkId = Annotated[
    str,
    Field(description="来自 search、outline 或 grep 结果的分块 ID。"),
]


@dataclass(frozen=True)
class MCPScope:
    """一次 MCP 调用可见的信源及其暖引擎。"""

    engine_manager: EngineManager
    sources: tuple[Source, ...]


_scope: contextvars.ContextVar[MCPScope | None] = contextvars.ContextVar(
    "sag_mcp_scope", default=None
)


def _require_scope() -> MCPScope:
    scope = _scope.get()
    if scope is None:
        raise RuntimeError("MCP 调用缺少知识库作用域")
    return scope


@contextlib.contextmanager
def use_scope(engine_manager: EngineManager, sources: Source | Iterable[Source]):
    """在上下文内绑定一个信源或一组信源。"""
    if hasattr(sources, "sag_source_config_id"):
        selected = (sources,)
    else:
        selected = tuple(sources)
    token = _scope.set(MCPScope(engine_manager=engine_manager, sources=selected))
    try:
        yield
    finally:
        _scope.reset(token)


def _selected_sources(scope: MCPScope, source_id: str = "") -> tuple[Source, ...]:
    target = (source_id or "").strip()
    if not target:
        return scope.sources
    return tuple(source for source in scope.sources if source.id == target)


def _source_title(source: Source) -> str:
    return f"{source.name}（source_id={source.id}）"


def _sections_to_text(sections: list, sources: tuple[Source, ...]) -> str:
    if not sections:
        return "（无相关资料）"
    by_config = {source.sag_source_config_id: source for source in sources}
    by_id = {source.id: source for source in sources}
    blocks = []
    for index, section in enumerate(sections, start=1):
        heading = getattr(section, "heading", None) or "片段"
        chunk_id = getattr(section, "chunk_id", None) or ""
        tag = f"（chunk_id={chunk_id}）" if chunk_id else ""
        source = by_config.get(getattr(section, "source_config_id", None))
        source = source or by_id.get(getattr(section, "source_id", None))
        source_line = f"来源：{_source_title(source)}\n" if source and len(sources) > 1 else ""
        blocks.append(
            f"[{index}] {heading}{tag}\n{source_line}{getattr(section, 'content', '')}"
        )
    return "\n\n".join(blocks)


async def _document_in_scope(
    scope: MCPScope, document_id: str
) -> tuple[Document, Source] | None:
    from sag_api.core.db import SessionLocal
    from sag_api.db.models import Document

    async with SessionLocal() as session:
        document = await session.get(Document, (document_id or "").strip())
    if document is None:
        return None
    source = next((item for item in scope.sources if item.id == document.source_id), None)
    return (document, source) if source is not None else None


def build_source_mcp(
    *,
    stateless_http: bool = False,
    transport_security: TransportSecuritySettings | None = None,
) -> FastMCP:
    """构造知识库 MCP server，具体作用域由 contextvar 在请求前注入。"""
    mcp = FastMCP(
        "sag-knowledge",
        instructions=(
            "SAG 知识库 MCP：默认检索全部信源，也可向工具传 source_id 限定范围。"
            "先用 list_sources/list_documents 了解资料范围，再用 search、grep、outline、"
            "read 和 get_chunk 获取可引用证据。回答请依据 search 返回的编号证据。"
        ),
        stateless_http=stateless_http,
        transport_security=transport_security,
    )

    @mcp.tool(
        title=MCP_TOOL_LABELS["list_sources"],
        description=MCP_TOOL_DESCRIPTIONS["list_sources"],
        annotations=READ_ONLY_TOOL_ANNOTATIONS,
    )
    async def list_sources() -> str:
        scope = _require_scope()
        if not scope.sources:
            return "（知识库还没有信源）"
        return "\n".join(
            f"- {_source_title(source)} · {source.document_count} 文档 · {source.chunk_count} 分块"
            for source in scope.sources
        )

    @mcp.tool(
        title=MCP_TOOL_LABELS["search"],
        description=MCP_TOOL_DESCRIPTIONS["search"],
        annotations=READ_ONLY_TOOL_ANNOTATIONS,
    )
    async def search(
        query: Annotated[str, Field(description="要查找的问题、主题或关键词。")],
        top_k: Annotated[
            int,
            Field(description="最多返回多少条证据；默认 8，服务端会限制在 1–50。"),
        ] = 8,
        source_id: SourceId = "",
    ) -> str:
        scope = _require_scope()
        selected = _selected_sources(scope, source_id)
        if not selected:
            return "（没有可检索的信源）" if not source_id else "（信源不存在或不在当前作用域）"
        normalized = (query or "").strip()
        if not normalized:
            return "（空查询）"
        outcome = await retrieve_relevant_sections(
            scope.engine_manager,
            selected,
            normalized,
            top_k=max(1, min(top_k, 50)),
        )
        return _sections_to_text(outcome.sections, selected)

    @mcp.tool(
        title=MCP_TOOL_LABELS["get_entity"],
        description=MCP_TOOL_DESCRIPTIONS["get_entity"],
        annotations=READ_ONLY_TOOL_ANNOTATIONS,
    )
    async def get_entity(
        name: Annotated[
            str,
            Field(description="人物、组织、概念等实体名称；支持完整名称或部分名称。"),
        ],
        source_id: SourceId = "",
    ) -> str:
        scope = _require_scope()
        selected = _selected_sources(scope, source_id)
        if not selected:
            return "（没有可查询的信源）" if not source_id else "（信源不存在或不在当前作用域）"
        target = (name or "").strip()
        if not target:
            return "（未找到该实体）"

        async def _one(source: Source) -> str | None:
            try:
                scid = source.sag_source_config_id
                entities = await scope.engine_manager.list_entities(
                    scid, source=source, limit=200
                )
                lowered = target.lower()
                match = next(
                    (entity for entity in entities if (entity.name or "").lower() == lowered),
                    None,
                )
                if match is None:
                    match = next(
                        (
                            entity
                            for entity in entities
                            if lowered in (entity.name or "").lower()
                        ),
                        None,
                    )
                if match is None:
                    return None
                snippets = await scope.engine_manager.entity_context(
                    scid, match.id, source=source, limit=6
                )
                body = "\n\n".join(snippets) if snippets else (match.description or "")
                prefix = f"来源：{_source_title(source)}\n" if len(selected) > 1 else ""
                return f"{prefix}实体「{match.name}」（{match.type}）：\n{body}".strip()
            except Exception:
                return None

        results = await asyncio.gather(*(_one(source) for source in selected))
        matches = [result for result in results if result]
        return "\n\n".join(matches) if matches else "（未找到该实体）"

    @mcp.tool(
        title=MCP_TOOL_LABELS["list_documents"],
        description=MCP_TOOL_DESCRIPTIONS["list_documents"],
        annotations=READ_ONLY_TOOL_ANNOTATIONS,
    )
    async def list_documents(source_id: SourceId = "") -> str:
        scope = _require_scope()
        selected = _selected_sources(scope, source_id)
        if not selected:
            return "（知识库还没有信源）" if not source_id else "（信源不存在或不在当前作用域）"
        from sqlalchemy import select

        from sag_api.core.db import SessionLocal
        from sag_api.db.models import Document

        source_ids = [source.id for source in selected]
        async with SessionLocal() as session:
            documents = list(
                (
                    await session.execute(
                        select(Document)
                        .where(Document.source_id.in_(source_ids))
                        .order_by(Document.created_at, Document.id)
                    )
                )
                .scalars()
                .all()
            )
        if not documents:
            return "（知识库还没有文档）"
        by_source: dict[str, list[Document]] = {item.id: [] for item in selected}
        for document in documents:
            by_source.setdefault(document.source_id, []).append(document)
        blocks = []
        for source in selected:
            rows = by_source.get(source.id) or []
            if not rows:
                continue
            lines = []
            for document in rows:
                status = getattr(document.status, "value", document.status)
                lines.append(
                    f"- {document.filename} · id={document.id} · {status} · "
                    f"{document.chunk_count} 分块"
                )
            header = f"## {_source_title(source)}\n" if len(selected) > 1 else ""
            blocks.append(header + "\n".join(lines))
        return "\n\n".join(blocks)

    @mcp.tool(
        title=MCP_TOOL_LABELS["outline"],
        description=MCP_TOOL_DESCRIPTIONS["outline"],
        annotations=READ_ONLY_TOOL_ANNOTATIONS,
    )
    async def outline(document_id: DocumentId) -> str:
        scope = _require_scope()
        match = await _document_in_scope(scope, document_id)
        if match is None:
            return "（未找到该文档）"
        document, source = match
        if not document.sag_source_id:
            return "（尚无大纲：文档可能仍在处理中）"
        rows = await scope.engine_manager.list_chunk_headings(
            source.sag_source_config_id,
            source=source,
            doc_sag_id=document.sag_source_id,
        )
        if not rows:
            return "（尚无大纲：文档可能仍在处理中）"
        return "\n".join(
            f"{row['rank']:>3}. {row['heading'] or '（无标题分块）'}"
            f"（chunk_id={row['chunk_id']}）"
            for row in rows
        )

    @mcp.tool(
        title=MCP_TOOL_LABELS["grep"],
        description=MCP_TOOL_DESCRIPTIONS["grep"],
        annotations=READ_ONLY_TOOL_ANNOTATIONS,
    )
    async def grep(
        pattern: Annotated[
            str,
            Field(description="要在原文中精确查找的文字；适合专名、编号、固定短语和代码。"),
        ],
        limit: Annotated[
            int,
            Field(description="最多返回多少处匹配；默认 20，服务端会限制在 1–100。"),
        ] = 20,
        source_id: SourceId = "",
    ) -> str:
        scope = _require_scope()
        selected = _selected_sources(scope, source_id)
        if not selected:
            return "（没有可查询的信源）" if not source_id else "（信源不存在或不在当前作用域）"
        needle = (pattern or "").strip()
        if not needle:
            return "（空匹配串）"
        bounded_limit = max(1, min(limit, 100))

        async def _one(source: Source) -> list[dict]:
            try:
                return await scope.engine_manager.grep_chunks(
                    source.sag_source_config_id,
                    needle,
                    source=source,
                    limit=bounded_limit,
                )
            except Exception:
                return []

        results = await asyncio.gather(*(_one(source) for source in selected))
        blocks = []
        for source, rows in zip(selected, results, strict=True):
            for row in rows:
                source_line = (
                    f"来源：{_source_title(source)}\n" if len(selected) > 1 else ""
                )
                blocks.append(
                    f"{row['heading'] or '片段'}（chunk_id={row['chunk_id']}）\n"
                    f"{source_line}{row['snippet']}"
                )
                if len(blocks) >= bounded_limit:
                    break
            if len(blocks) >= bounded_limit:
                break
        if not blocks:
            return "（未匹配到内容）"
        return "\n\n".join(f"[{index}] {block}" for index, block in enumerate(blocks, 1))

    @mcp.tool(
        title=MCP_TOOL_LABELS["read"],
        description=MCP_TOOL_DESCRIPTIONS["read"],
        annotations=READ_ONLY_TOOL_ANNOTATIONS,
    )
    async def read(
        document_id: DocumentId,
        offset: Annotated[
            int,
            Field(description="从第几行开始读取；首行为 1，默认 1。"),
        ] = 1,
        limit: Annotated[
            int,
            Field(description="本次读取多少行；默认 120，服务端最多返回 500 行。"),
        ] = 120,
    ) -> str:
        scope = _require_scope()
        match = await _document_in_scope(scope, document_id)
        if match is None:
            return "（未找到该文档）"
        document, source = match
        import os

        if not document.storage_path or not os.path.isfile(document.storage_path):
            return "（原始文件不存在或已清理）"
        try:
            with open(document.storage_path, encoding="utf-8", errors="replace") as file:
                lines = file.readlines()
        except OSError:
            return "（文件读取失败）"
        start = max(0, offset - 1)
        page = lines[start : start + max(1, min(limit, 500))]
        if not page:
            return f"（超出范围：全文共 {len(lines)} 行）"
        body = "".join(f"{start + index + 1:>5}\t{line}" for index, line in enumerate(page))
        source_line = f"来源：{_source_title(source)}\n" if len(scope.sources) > 1 else ""
        return (
            f"{document.filename} · 第 {start + 1}-{start + len(page)} 行 / "
            f"共 {len(lines)} 行\n{source_line}{body}"
        )

    @mcp.tool(
        title=MCP_TOOL_LABELS["get_chunk"],
        description=MCP_TOOL_DESCRIPTIONS["get_chunk"],
        annotations=READ_ONLY_TOOL_ANNOTATIONS,
    )
    async def get_chunk(chunk_id: ChunkId, source_id: SourceId = "") -> str:
        scope = _require_scope()
        selected = _selected_sources(scope, source_id)
        if not selected:
            return "（没有可查询的信源）" if not source_id else "（信源不存在或不在当前作用域）"
        cid = (chunk_id or "").strip()
        if not cid:
            return "（缺少 chunk_id）"

        async def _one(source: Source):
            try:
                chunk = await scope.engine_manager.get_chunk(
                    source.sag_source_config_id, cid, source=source
                )
                return source, chunk
            except Exception:
                return source, None

        results = await asyncio.gather(*(_one(source) for source in selected))
        found = next(((source, chunk) for source, chunk in results if chunk is not None), None)
        if found is None:
            return "（未找到该分块）"
        source, chunk = found
        heading = (chunk.heading or "").strip()
        body = f"{heading}\n\n{chunk.content}".strip() if heading else chunk.content
        return f"来源：{_source_title(source)}\n\n{body}" if len(selected) > 1 else body

    return mcp


_singleton: FastMCP | None = None


def get_source_mcp() -> FastMCP:
    """返回 stdio/进程内调用复用的 MCP server。"""
    global _singleton
    if _singleton is None:
        _singleton = build_source_mcp()
    return _singleton


async def serve_stdio(source_id: str | None = None) -> None:
    """运行 stdio server；未提供 source_id 时开放全部信源。"""
    from sqlalchemy import select

    from sag_api.core.config import settings
    from sag_api.core.db import SessionLocal
    from sag_api.db.models import Source
    from sag_api.sag import EngineManager

    engine_manager = EngineManager(settings)
    async with SessionLocal() as session:
        statement = select(Source).order_by(Source.created_at, Source.id)
        if source_id:
            statement = statement.where(Source.id == source_id)
        sources = tuple((await session.execute(statement)).scalars().all())
    if source_id and not sources:
        raise SystemExit(f"信源不存在：{source_id}")

    mcp = get_source_mcp()
    try:
        with use_scope(engine_manager, sources):
            await mcp.run_stdio_async()
    finally:
        await engine_manager.aclose_all()


def _main() -> None:
    import os

    source_id = os.environ.get("SAG_MCP_SOURCE_ID", "").strip() or None
    asyncio.run(serve_stdio(source_id))


if __name__ == "__main__":
    _main()
