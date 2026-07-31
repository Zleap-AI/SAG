from __future__ import annotations

from pydantic import BaseModel


class ChunkOutlineOut(BaseModel):
    rank: int
    heading: str
    chunk_id: str


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
