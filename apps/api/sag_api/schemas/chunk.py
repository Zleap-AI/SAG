from __future__ import annotations

from pydantic import BaseModel, Field


class ChunkOutlineOut(BaseModel):
    rank: int
    heading: str
    chunk_id: str
    # 分块所属章节的 Markdown 标题层级（1–6）与从顶层到该章节的标题路径；
    # 无法在正文中定位章节时为 None / 空。
    level: int | None = None
    path: list[str] = Field(default_factory=list)


class OutlineOut(BaseModel):
    document_id: str
    filename: str
    outline: list[ChunkOutlineOut]


class GrepMatchOut(BaseModel):
    chunk_id: str
    heading: str
    snippet: str
    source_id: str | None = None
    source_name: str | None = None


class GrepResponse(BaseModel):
    pattern: str
    matches: list[GrepMatchOut]
    count: int


class ReadResponse(BaseModel):
    document_id: str
    filename: str
    total_lines: int
    offset: int
    limit: int
    lines: list[str]


class EntityContextOut(BaseModel):
    entity_id: str
    name: str
    type: str
    description: str
    context: str
    source_id: str
    source_name: str
