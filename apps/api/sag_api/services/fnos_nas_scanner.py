"""Bounded, deterministic, symlink-safe discovery of documents under an authorized root."""

from __future__ import annotations

import asyncio
import hashlib
import os
import stat
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sag_api.core.config import settings
from sag_api.core.errors import ValidationError
from sag_api.db.models import Document, Source
from sag_api.fnos.identity import GatewayIdentity
from sag_api.fnos.nas_registry import FnOSNasScanRegistry, NasScanEntry
from sag_api.fnos.open_api import FnOSOpenAPIClient
from sag_api.services.document_validation import (
    DocumentFileRejection,
    DocumentFileValidationError,
    validate_document_file,
)
from sag_api.services.fnos_nas_access import ResolvedNasRoot

SCAN_LIMITS = {
    "wall_seconds": 20,
    "visited_entries": 20_000,
    "returned_files": 5_000,
    "max_depth": 32,
    "acl_batch_size": 100,
}

ScanState = Literal["new", "changed", "imported", "unsupported", "too_large", "unreadable"]


@dataclass(frozen=True)
class NasScanFile:
    selection_token: str | None
    name: str
    display_path: str
    extension: str
    size_bytes: int
    modified_at: datetime
    state: ScanState
    selected_by_default: bool
    document_id: str | None


@dataclass(frozen=True)
class NasScanSummary:
    visited: int = 0
    eligible: int = 0
    new: int = 0
    changed: int = 0
    imported: int = 0
    unsupported: int = 0
    too_large: int = 0
    unreadable: int = 0


@dataclass(frozen=True)
class NasScanResult:
    scan_id: str
    folder: str
    files: list[NasScanFile]
    summary: NasScanSummary
    truncated: bool
    truncated_reason: str | None
    selection_expires_at: datetime


@dataclass(frozen=True)
class _WalkFile:
    path: Path
    display_path: str
    size_bytes: int
    mtime_ns: int


@dataclass(frozen=True)
class _WalkResult:
    files: list[_WalkFile]
    visited: int
    truncated: bool
    reason: str | None


@dataclass(frozen=True)
class _Draft:
    output: NasScanFile
    registry_entry: NasScanEntry | None


def nas_origin_key(canonical_root: Path, canonical_path: Path) -> str:
    return hashlib.sha256(f"{canonical_root}\0{canonical_path}".encode()).hexdigest()


class FnOSNasScanner:
    def __init__(
        self,
        open_api: FnOSOpenAPIClient,
        registry: FnOSNasScanRegistry,
        *,
        settings_obj=settings,
        limits: dict[str, int | float] | None = None,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        self._open_api = open_api
        self._registry = registry
        self._settings = settings_obj
        self._limits = {**SCAN_LIMITS, **(limits or {})}
        self._monotonic = monotonic or time.monotonic

    async def scan(
        self,
        session: AsyncSession,
        *,
        identity: GatewayIdentity,
        source: Source,
        root: ResolvedNasRoot,
        recursive: bool,
    ) -> NasScanResult:
        cancel = threading.Event()
        try:
            walked = await asyncio.to_thread(self._walk, root.path, recursive, cancel)
        except asyncio.CancelledError:
            cancel.set()
            raise

        root_path = self._validate_root(root.path)
        drafts: list[_Draft] = []
        acl_candidates: list[_WalkFile] = []
        rejected: dict[str, DocumentFileRejection] = {}
        for item in walked.files:
            try:
                validate_document_file(item.path.name, item.size_bytes, self._settings)
            except DocumentFileValidationError as error:
                rejected[str(item.path)] = error.reason
            else:
                acl_candidates.append(item)

        readable: dict[str, bool] = {str(item.path): True for item in acl_candidates}
        if root.source == "host_api" and acl_candidates:
            readable = {}
            batch_size = int(self._limits["acl_batch_size"])
            for offset in range(0, len(acl_candidates), batch_size):
                paths = [str(item.path) for item in acl_candidates[offset : offset + batch_size]]
                for result in await self._open_api.check_user_acl(identity.uid, paths):
                    readable[result.path] = result.readable

        eligible = [item for item in acl_candidates if readable.get(str(item.path), False)]
        key_by_path = {
            str(item.path): nas_origin_key(root_path, item.path)
            for item in eligible
        }
        existing_by_key: dict[str, Document] = {}
        if key_by_path:
            documents = (
                await session.scalars(
                    select(Document).where(
                        Document.source_id == source.id,
                        Document.origin_kind == "fnos_shared",
                        Document.origin_key.in_(list(key_by_path.values())),
                    )
                )
            ).all()
            existing_by_key = {document.origin_key: document for document in documents if document.origin_key}

        for item in walked.files:
            extension = item.path.suffix.lower()
            common = dict(
                selection_token=None,
                name=item.path.name,
                display_path=item.display_path,
                extension=extension,
                size_bytes=item.size_bytes,
                modified_at=datetime.fromtimestamp(item.mtime_ns / 1_000_000_000, tz=UTC),
                selected_by_default=False,
                document_id=None,
            )
            rejection = rejected.get(str(item.path))
            if rejection is not None:
                state: ScanState = "too_large" if rejection is DocumentFileRejection.TOO_LARGE else "unsupported"
                drafts.append(_Draft(NasScanFile(state=state, **common), None))
                continue
            if not readable.get(str(item.path), False):
                drafts.append(_Draft(NasScanFile(state="unreadable", **common), None))
                continue

            origin_key = key_by_path[str(item.path)]
            existing = existing_by_key.get(origin_key)
            if existing is None:
                state = "new"
            elif existing.origin_size_bytes == item.size_bytes and existing.origin_mtime_ns == item.mtime_ns:
                state = "imported"
            else:
                state = "changed"
            output = NasScanFile(
                state=state,
                selected_by_default=state in {"new", "changed"},
                document_id=existing.id if existing is not None else None,
                **{key: value for key, value in common.items() if key not in {"selected_by_default", "document_id"}},
            )
            registry_entry = NasScanEntry(
                canonical_root=str(root_path),
                canonical_path=str(item.path),
                display_path=item.display_path,
                size_bytes=item.size_bytes,
                mtime_ns=item.mtime_ns,
                folder_source=root.source,
            )
            drafts.append(_Draft(output, registry_entry))

        selectable = [draft.registry_entry for draft in drafts if draft.registry_entry is not None]
        registered = self._registry.register(identity.uid, source.id, selectable)
        tokens = iter(registered.selection_tokens)
        files = [
            replace(draft.output, selection_token=next(tokens))
            if draft.registry_entry is not None
            else draft.output
            for draft in drafts
        ]
        summary = NasScanSummary(
            visited=walked.visited,
            eligible=sum(file.state in {"new", "changed", "imported"} for file in files),
            new=sum(file.state == "new" for file in files),
            changed=sum(file.state == "changed" for file in files),
            imported=sum(file.state == "imported" for file in files),
            unsupported=sum(file.state == "unsupported" for file in files),
            too_large=sum(file.state == "too_large" for file in files),
            unreadable=sum(file.state == "unreadable" for file in files),
        )
        return NasScanResult(
            scan_id=registered.scan_id,
            folder=root.display_path,
            files=files,
            summary=summary,
            truncated=walked.truncated,
            truncated_reason=walked.reason,
            selection_expires_at=datetime.fromtimestamp(registered.expires_at, tz=UTC),
        )

    def _validate_root(self, root: Path) -> Path:
        try:
            metadata = os.lstat(root)
            resolved = root.resolve(strict=True)
        except OSError as error:
            raise ValidationError("扫描目录不存在或不可读") from error
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise ValidationError("扫描目录必须是非链接目录")
        if resolved != root.absolute():
            raise ValidationError("扫描目录路径不安全")
        return resolved

    def _walk(self, root: Path, recursive: bool, cancel: threading.Event) -> _WalkResult:
        canonical_root = self._validate_root(root)
        started = self._monotonic()
        files: list[_WalkFile] = []
        visited = 0
        reason: str | None = None

        def visit(directory: Path, depth: int) -> None:
            nonlocal visited, reason
            try:
                with os.scandir(directory) as iterator:
                    entries = sorted(iterator, key=lambda item: (item.name.casefold(), item.name))
            except OSError:
                return
            for entry in entries:
                if reason is not None:
                    return
                if cancel.is_set():
                    reason = "cancelled"
                    return
                if self._monotonic() - started >= float(self._limits["wall_seconds"]):
                    reason = "time_limit"
                    return
                if visited >= int(self._limits["visited_entries"]):
                    reason = "entry_limit"
                    return
                visited += 1
                if entry.name.startswith("."):
                    continue
                try:
                    metadata = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                if stat.S_ISLNK(metadata.st_mode):
                    continue
                if stat.S_ISDIR(metadata.st_mode):
                    if recursive:
                        if depth >= int(self._limits["max_depth"]):
                            reason = "depth_limit"
                            return
                        visit(Path(entry.path), depth + 1)
                    continue
                if not stat.S_ISREG(metadata.st_mode):
                    continue
                if len(files) >= int(self._limits["returned_files"]):
                    reason = "file_limit"
                    return
                try:
                    canonical_path = Path(entry.path).resolve(strict=True)
                    if os.path.commonpath((canonical_root, canonical_path)) != str(canonical_root):
                        continue
                except (OSError, ValueError):
                    continue
                files.append(
                    _WalkFile(
                        path=canonical_path,
                        display_path=canonical_path.relative_to(canonical_root).as_posix(),
                        size_bytes=metadata.st_size,
                        mtime_ns=metadata.st_mtime_ns,
                    )
                )

        visit(canonical_root, 0)
        return _WalkResult(files, visited, reason is not None, reason)
