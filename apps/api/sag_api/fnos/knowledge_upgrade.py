"""Metadata-only quarantine of Native 0.7.1 knowledge on first 0.13 startup."""

from __future__ import annotations

import os
import uuid
from pathlib import Path

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from sag_api.db.models import Document, Job, Message, Setting, Source
from sag_api.enums import DocumentStatus, JobStatus, JobType

_UPGRADE_KEY = "fnos_knowledge_engine_0_13"


def stale_internal_citations(citations: list) -> list:
    """Keep historical citation copy while disabling links to old chunk IDs."""
    return [
        {**citation, "stale": True}
        if isinstance(citation, dict) and citation.get("kind") != "external"
        else citation
        for citation in citations
    ]


def original_available(path: str | None, uploads_dir: Path) -> bool:
    """Only a regular private upload copy can be used for automatic reingestion."""
    if not path:
        return False
    try:
        uploads = uploads_dir.resolve(strict=True)
        candidate = Path(path)
        if candidate.is_symlink() or not candidate.is_file():
            return False
        return candidate.resolve(strict=True).is_relative_to(uploads)
    except (OSError, ValueError):
        return False


async def mark_legacy_knowledge_pending(
    session: AsyncSession, legacy_engine_dir: Path, uploads_dir: Path
) -> int:
    """Freeze old jobs and flag derived knowledge, without any engine or model call.

    The per-tenant business database and the legacy engine directory remain intact.
    The one-time marker and all status changes commit together, so a crash before
    commit repeats safely on the next worker start.
    """
    marker = await session.scalar(
        select(Setting).where(Setting.scope == "global", Setting.key == _UPGRADE_KEY)
    )
    if marker is not None:
        return 0
    if legacy_engine_dir.is_symlink():
        raise ValueError("legacy engine directory must not be a symlink")
    if not legacy_engine_dir.is_dir():
        return 0

    documents = list((await session.scalars(select(Document))).all())
    for document in documents:
        document.status = DocumentStatus.STALE
        document.knowledge_state = (
            "pending" if original_available(document.storage_path, uploads_dir) else "needs_file"
        )
    for job in (await session.scalars(select(Job))).all():
        if job.status in {JobStatus.QUEUED, JobStatus.RUNNING}:
            job.status = JobStatus.PAUSED
    for source in (await session.scalars(select(Source))).all():
        source.chunk_count = 0
        source.event_count = 0
    for message in (await session.scalars(select(Message))).all():
        if isinstance(message.citations, list) and message.citations:
            message.citations = stale_internal_citations(message.citations)
    session.add(
        Setting(scope="global", key=_UPGRADE_KEY, value={"engine": "0.13.0", "legacy_retained": True})
    )
    await session.commit()
    return len(documents)


async def queue_legacy_reingest(session: AsyncSession, uploads_dir: Path, job_queue) -> dict[str, int]:
    """Queue each retained private original at most once per active attempt."""
    queued_jobs: list[Job] = []
    needs_file = 0
    documents = list(
        (await session.scalars(select(Document).where(Document.knowledge_state.is_not(None)))).all()
    )
    for document in documents:
        if document.knowledge_state == "needs_file" and not original_available(
            document.storage_path, uploads_dir
        ):
            needs_file += 1
            continue
        if document.knowledge_state not in {"pending", "needs_file", "failed"}:
            continue
        if not original_available(document.storage_path, uploads_dir):
            document.knowledge_state = "needs_file"
            needs_file += 1
            continue
        claimed = await session.execute(
            update(Document)
            .where(
                Document.id == document.id,
                Document.knowledge_state == document.knowledge_state,
            )
            .values(
                status=DocumentStatus.PENDING,
                knowledge_state="queued",
                progress=0,
                chunk_count=0,
                event_count=0,
                token_usage=0,
                sag_source_id=None,
                error=None,
            )
            .execution_options(synchronize_session=False)
        )
        if claimed.rowcount != 1:
            continue
        queued_jobs.append(
            Job(
                type=JobType.PROCESS_DOCUMENT,
                status=JobStatus.QUEUED,
                source_id=document.source_id,
                document_id=document.id,
                payload={"knowledge_upgrade_reingest": True},
            )
        )
    session.add_all(queued_jobs)
    await session.commit()
    for job in queued_jobs:
        await job_queue.enqueue(job.id)
    return {"queued": len(queued_jobs), "needs_file": needs_file}


async def reingest_status(session: AsyncSession) -> dict:
    documents = list(
        (await session.scalars(select(Document).where(Document.knowledge_state.is_not(None)))).all()
    )
    states = {state: 0 for state in ("pending", "needs_file", "queued", "running", "ready", "failed")}
    missing = []
    for document in documents:
        state = document.knowledge_state
        if state in states:
            states[state] += 1
        if state == "needs_file":
            missing.append(
                {"source_id": document.source_id, "document_id": document.id, "filename": document.filename}
            )
    return {"required": bool(documents), "total": len(documents), "states": states, "missing": missing}


async def replace_missing_original(
    session: AsyncSession,
    document_id: str,
    *,
    filename: str,
    content_type: str,
    data: bytes,
    uploads_dir: Path,
) -> None:
    """Attach a replacement private copy to the same legacy document row."""
    document = await session.get(Document, document_id)
    if document is None or document.knowledge_state != "needs_file":
        raise ValueError("document is not awaiting a replacement original")
    if Path(filename).name != document.filename:
        raise ValueError("replacement filename must match the original document")
    uploads_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    private_root = uploads_dir.resolve(strict=True)
    if uploads_dir.is_symlink() or Path(document.source_id).name != document.source_id:
        raise ValueError("invalid private upload destination")
    dest_dir = private_root / document.source_id
    if dest_dir.is_symlink():
        raise ValueError("private upload destination is a symlink")
    dest_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    if dest_dir.resolve(strict=True).parent != private_root:
        raise ValueError("private upload destination escapes root")
    destination = dest_dir / f"{document.id}_{uuid.uuid4().hex}_{document.filename}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(destination, flags, 0o600)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        document.storage_path = str(destination)
        document.size_bytes = len(data)
        document.content_type = content_type
        document.knowledge_state = "pending"
        await session.commit()
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
