"""Securely copy selected fnOS NAS files into private SAG-owned storage."""

from __future__ import annotations

import hashlib
import mimetypes
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sag_api.core.config import settings
from sag_api.core.error_taxonomy import ErrorCode
from sag_api.core.errors import ConflictError, NotFoundError, ValidationError
from sag_api.db.base import new_id
from sag_api.db.models import Document, Job, Source
from sag_api.fnos.identity import GatewayIdentity
from sag_api.fnos.nas_registry import NasScanEntry
from sag_api.fnos.open_api import FnOSOpenAPIClient
from sag_api.jobs import JobQueue
from sag_api.services.document_service import (
    StagedDocumentOrigin,
    register_document_from_staged_file,
    stage_document_replacement,
)
from sag_api.services.document_validation import validate_document_file
from sag_api.services.fnos_nas_access import FnOSNasAccessService, ResolvedNasRoot
from sag_api.services.fnos_nas_scanner import nas_origin_key

COPY_CHUNK_SIZE = 1024 * 1024


@dataclass(frozen=True)
class NasImportOutcome:
    display_path: str
    outcome: str
    document_id: str | None
    reason: str | None = None


class FnOSNasImporter:
    def __init__(
        self,
        access: FnOSNasAccessService,
        open_api: FnOSOpenAPIClient,
        *,
        upload_dir: str | Path,
        job_queue: JobQueue,
    ) -> None:
        self._access = access
        self._open_api = open_api
        self._upload_dir = Path(upload_dir)
        self._job_queue = job_queue

    async def import_one(
        self,
        session: AsyncSession,
        job: Job,
        entry: NasScanEntry,
        *,
        identity: GatewayIdentity,
    ) -> NasImportOutcome:
        source = await session.get(Source, job.source_id) if job.source_id else None
        if source is None:
            raise NotFoundError("信源不存在")
        root_path = Path(entry.canonical_root)
        source_path = Path(entry.canonical_path)
        root = ResolvedNasRoot(
            path=root_path,
            authorized_path=str(root_path),
            display_path="",
            source=entry.folder_source,
            folder_id="",
        )
        await self._access.revalidate_root(session, identity, root)
        self._validate_path(root_path, source_path)
        if entry.folder_source == "host_api":
            acl = await self._open_api.check_user_acl(identity.uid, [str(source_path)])
            if not acl or not acl[0].readable:
                raise ConflictError(
                    "NAS 文件不再可读",
                    code=ErrorCode.NAS_FILE_UNREADABLE,
                )

        try:
            before = os.lstat(source_path)
        except OSError as error:
            raise ConflictError("NAS 文件已发生变化", code=ErrorCode.NAS_FILE_CHANGED) from error
        if not stat.S_ISREG(before.st_mode):
            raise ValidationError("NAS 文件路径不安全")
        if before.st_size != entry.size_bytes or before.st_mtime_ns != entry.mtime_ns:
            raise ConflictError("NAS 文件已发生变化", code=ErrorCode.NAS_FILE_CHANGED)
        validate_document_file(source_path.name, before.st_size, settings)

        stage_dir = self._upload_dir / ".nas-stage" / job.id
        stage_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        stage_path = stage_dir / f"{new_id()}.stage"
        retain_stage = False
        try:
            digest, copied = self._copy_once(source_path, stage_path, before)
            if copied != entry.size_bytes:
                raise ConflictError("NAS 文件已发生变化", code=ErrorCode.NAS_FILE_CHANGED)
            origin_key = nas_origin_key(root_path, source_path)
            origin = StagedDocumentOrigin(
                kind="fnos_shared",
                key=origin_key,
                path=str(source_path),
                display_path=entry.display_path,
                size_bytes=copied,
                mtime_ns=before.st_mtime_ns,
                sha256=digest,
            )
            existing = await session.scalar(
                select(Document).where(
                    Document.source_id == source.id,
                    Document.origin_kind == origin.kind,
                    Document.origin_key == origin.key,
                )
            )
            if existing is not None:
                if existing.origin_sha256 == digest:
                    stage_path.unlink(missing_ok=True)
                    existing.origin_size_bytes = copied
                    existing.origin_mtime_ns = before.st_mtime_ns
                    await session.commit()
                    return NasImportOutcome(entry.display_path, "skipped", existing.id)
                await stage_document_replacement(
                    session,
                    source,
                    existing,
                    staged_path=stage_path,
                    origin=origin,
                    job_queue=self._job_queue,
                )
                retain_stage = True
                return NasImportOutcome(entry.display_path, "updated", existing.id)

            content_type = mimetypes.guess_type(source_path.name)[0] or "application/octet-stream"
            document, child = await register_document_from_staged_file(
                session,
                source,
                staged_path=stage_path,
                filename=source_path.name,
                content_type=content_type,
                size_bytes=copied,
                origin=origin,
                upload_dir=self._upload_dir,
                job_queue=self._job_queue,
            )
            if child is None:
                if document.origin_sha256 == digest:
                    return NasImportOutcome(entry.display_path, "skipped", document.id)
                raise ConflictError("NAS 文档已有新版本", code="nas_document_changed")
            return NasImportOutcome(entry.display_path, "created", document.id)
        finally:
            if not retain_stage:
                stage_path.unlink(missing_ok=True)
                try:
                    stage_dir.rmdir()
                    stage_dir.parent.rmdir()
                except OSError:
                    pass

    def _validate_path(self, root: Path, path: Path) -> None:
        if not root.is_absolute() or not path.is_absolute() or ".." in path.parts:
            raise ValidationError("NAS 文件路径不安全")
        try:
            root_metadata = os.lstat(root)
        except OSError as error:
            raise ValidationError("NAS 文件路径不安全") from error
        if stat.S_ISLNK(root_metadata.st_mode) or not stat.S_ISDIR(root_metadata.st_mode):
            raise ValidationError("NAS 文件路径不安全")
        try:
            relative = path.relative_to(root)
        except ValueError:
            raise ValidationError("NAS 文件路径不安全") from None
        current = root
        for index, component in enumerate(relative.parts):
            current = current / component
            try:
                metadata = os.lstat(current)
            except OSError as error:
                raise ValidationError("NAS 文件路径不安全") from error
            if stat.S_ISLNK(metadata.st_mode):
                raise ValidationError("NAS 文件路径不安全")
            if index < len(relative.parts) - 1 and not stat.S_ISDIR(metadata.st_mode):
                raise ValidationError("NAS 文件路径不安全")

    def _copy_once(self, source: Path, destination: Path, before: os.stat_result) -> tuple[str, int]:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            source_fd = os.open(source, flags)
        except OSError as error:
            raise ValidationError("NAS 文件路径不安全") from error
        digest = hashlib.sha256()
        copied = 0
        try:
            after_open = os.fstat(source_fd)
            if (
                not stat.S_ISREG(after_open.st_mode)
                or after_open.st_dev != before.st_dev
                or after_open.st_ino != before.st_ino
                or after_open.st_size != before.st_size
                or after_open.st_mtime_ns != before.st_mtime_ns
            ):
                raise ConflictError("NAS 文件已发生变化", code=ErrorCode.NAS_FILE_CHANGED)
            with os.fdopen(source_fd, "rb", closefd=False) as source_handle, destination.open("xb") as target:
                while chunk := source_handle.read(COPY_CHUNK_SIZE):
                    target.write(chunk)
                    digest.update(chunk)
                    copied += len(chunk)
                target.flush()
                os.fsync(target.fileno())
            after_copy = os.fstat(source_fd)
            if (
                after_copy.st_size != before.st_size
                or after_copy.st_mtime_ns != before.st_mtime_ns
                or copied != before.st_size
            ):
                raise ConflictError("NAS 文件已发生变化", code=ErrorCode.NAS_FILE_CHANGED)
            return digest.hexdigest(), copied
        finally:
            os.close(source_fd)
