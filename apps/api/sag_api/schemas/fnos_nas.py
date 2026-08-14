"""Public, path-redacted contracts for fnOS NAS discovery and import."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from sag_api.enums import JobStatus

OpaqueSelectionToken = Annotated[str, Field(min_length=1, max_length=2048)]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)


class NasLimitsOut(_StrictModel):
    max_files: int = Field(ge=1)
    max_import_files: int = Field(ge=1, le=500)
    max_import_bytes: int = Field(ge=1)
    max_file_bytes: int = Field(ge=1)


class NasFolderOut(_StrictModel):
    id: str = Field(min_length=1, max_length=512)
    display_path: str = Field(min_length=1, max_length=2048)
    source: Literal["host_api", "legacy_manual"]
    readable: bool


class NasStatusOut(_StrictModel):
    eligible: bool
    mode: Literal["automatic", "legacy_manual", "unavailable"]
    system_version: str | None = Field(default=None, max_length=64)
    automatic_authorization: bool
    folders: list[NasFolderOut] = Field(max_length=5000)
    limits: NasLimitsOut
    reason: str | None = Field(default=None, max_length=128)


class NasLegacyFolderCreate(_StrictModel):
    path: str = Field(min_length=1, max_length=2048)


class NasScanRequest(_StrictModel):
    source_id: str = Field(min_length=1, max_length=64)
    folder_id: str = Field(min_length=1, max_length=512)
    recursive: bool = True


class NasScanFileOut(_StrictModel):
    selection_token: str | None = Field(default=None, max_length=2048)
    name: str = Field(min_length=1, max_length=512)
    display_path: str = Field(min_length=1, max_length=2048)
    extension: str = Field(max_length=32)
    size_bytes: int = Field(ge=0)
    modified_at: datetime
    state: Literal["new", "changed", "imported", "unsupported", "too_large", "unreadable"]
    selected_by_default: bool
    document_id: str | None = Field(default=None, max_length=64)


class NasScanSummaryOut(_StrictModel):
    visited: int = Field(ge=0)
    eligible: int = Field(ge=0)
    new: int = Field(ge=0)
    changed: int = Field(ge=0)
    imported: int = Field(ge=0)
    unsupported: int = Field(ge=0)
    too_large: int = Field(ge=0)
    unreadable: int = Field(ge=0)


class NasScanOut(_StrictModel):
    scan_id: str = Field(min_length=1, max_length=128)
    folder: str = Field(min_length=1, max_length=2048)
    files: list[NasScanFileOut] = Field(max_length=5000)
    summary: NasScanSummaryOut
    truncated: bool
    truncated_reason: str | None = Field(default=None, max_length=128)
    selection_expires_at: datetime


class NasImportRequest(_StrictModel):
    source_id: str = Field(min_length=1, max_length=64)
    selection_tokens: list[OpaqueSelectionToken] = Field(min_length=1, max_length=500)


class NasImportAccepted(_StrictModel):
    job_id: str = Field(min_length=1, max_length=64)
    accepted: int = Field(ge=1, le=500)


class NasImportItemOut(_StrictModel):
    display_path: str = Field(min_length=1, max_length=2048)
    outcome: Literal["created", "updated", "skipped", "failed"]
    document_id: str | None = Field(default=None, max_length=64)
    reason: str | None = Field(default=None, max_length=128)


class NasImportProgressOut(_StrictModel):
    id: str = Field(min_length=1, max_length=64)
    status: JobStatus
    progress: float = Field(ge=0, le=1)
    total: int = Field(ge=0, le=500)
    completed: int = Field(ge=0, le=500)
    created: int = Field(ge=0, le=500)
    updated: int = Field(ge=0, le=500)
    skipped: int = Field(ge=0, le=500)
    failed: int = Field(ge=0, le=500)
    results: list[NasImportItemOut] = Field(default_factory=list, max_length=500)
