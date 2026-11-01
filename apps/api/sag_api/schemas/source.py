from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from sag_api.enums import ConnectorKind, SearchStrategy, SourceStatus, SourceType
from sag_api.schemas.job import JobOut


class ConnectorOut(BaseModel):
    kind: str
    title: str
    description: str
    supports_sync: bool
    config_fields: list[dict[str, Any]]


class SourceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    engine_kind: Literal["sag1", "sag2"] = "sag1"
    name: str = Field(min_length=1, max_length=200)
    description: str = ""
    connector_kind: ConnectorKind = ConnectorKind.FILE_UPLOAD
    config: dict[str, Any] = Field(default_factory=dict)


class SourceUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, max_length=200)
    description: str | None = None
    status: SourceStatus | None = None


class SourceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    engine_kind: Literal["sag1", "sag2"] = "sag1"
    name: str
    description: str
    source_type: SourceType
    connector_kind: ConnectorKind
    status: SourceStatus
    document_count: int
    chunk_count: int
    event_count: int
    default_search_strategy: SearchStrategy | None = None
    created_at: datetime
    updated_at: datetime
    deletion_job: JobOut | None = None
