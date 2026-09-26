"""OCTX transfer 的共享辅助 —— 从 `octx_transfer_service` 抽出。

只含无模块级打桩依赖的逻辑：进度门与进度换算、job 登记、影子身份、
导入/导出的持久化辅助。导入侧与导出侧都从这里取，避免两侧各自复制。
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import shutil
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from sag_api.core.error_taxonomy import ErrorCode, ErrorLayer, ErrorStage
from sag_api.core.errors import ConflictError, ValidationError
from sag_api.db.base import new_id
from sag_api.db.models import (
    Document,
    Job,
    OctxTransfer,
)
from sag_api.enums import (
    DocumentStatus,
    JobStatus,
    JobType,
    OctxTransferStatus,
)
from sag_api.octx.storage import FileSignature, OctxStorage, StoredUpload
from sag_api.services.octx_checkpoint import merge_checkpoint

logger = logging.getLogger(__name__)

_EXPORT_SNAPSHOT_RANGES = {
    "documents": (0.10, 0.15),
    "chunks": (0.15, 0.23),
    "events": (0.23, 0.30),
    "entities": (0.30, 0.38),
    "event_entities": (0.38, 0.45),
}
_EXPORT_VECTOR_ROLES = (
    "chunk.heading",
    "chunk.content",
    "event.title",
    "event.content",
    "entity.name",
    "event_entity.relation",
)


@dataclass(slots=True)
class _VectorProgressGate:
    total: int
    interval_seconds: float
    last_completed: int = -1
    last_persisted_at: float | None = None
    last_stage: tuple[str, str] | None = None

    def should_persist(
        self,
        kind: str,
        mode: str,
        completed: int,
        *,
        now: float | None = None,
    ) -> bool:
        timestamp = time.monotonic() if now is None else now
        stage = (kind, mode)
        threshold = max(1, math.ceil(self.total * 0.01))
        should_write = (
            self.last_persisted_at is None
            or stage != self.last_stage
            or completed - self.last_completed >= threshold
            or timestamp - self.last_persisted_at >= self.interval_seconds
        )
        if should_write:
            self.last_completed = completed
            self.last_persisted_at = timestamp
            self.last_stage = stage
        return should_write


def _export_progress(detail: dict[str, Any]) -> float:
    phase = str(detail.get("phase") or "")
    if phase == "snapshot_complete":
        return 0.59
    completed = max(0, int(detail.get("completed") or 0))
    total = max(1, int(detail.get("total") or 0))
    fraction = min(1.0, completed / total)
    kind = str(detail.get("kind") or "")
    if phase == "vectors":
        try:
            index = _EXPORT_VECTOR_ROLES.index(kind)
        except ValueError:
            index = 0
        width = 0.14 / len(_EXPORT_VECTOR_ROLES)
        return 0.45 + width * (index + fraction)
    start, end = _EXPORT_SNAPSHOT_RANGES.get(kind, (0.10, 0.45))
    return start + (end - start) * fraction


async def _create_job(
    session: AsyncSession,
    transfer: OctxTransfer,
    job_type: JobType,
    *,
    source_id: str | None = None,
) -> Job:
    job = Job(
        type=job_type,
        status=JobStatus.QUEUED,
        source_id=source_id,
        payload={"transfer_id": transfer.id},
    )
    session.add(job)
    await session.flush()
    return job


def _stored_upload(transfer: OctxTransfer, storage: OctxStorage) -> StoredUpload:
    signature = dict(transfer.input_signature or {})
    if not transfer.staging_key or not transfer.upload_sha256 or not signature:
        raise ValidationError("OCTX transfer has no immutable staged upload")
    file_signature = FileSignature(
        device=int(signature["device"]),
        inode=int(signature["inode"]),
        size=int(signature["size"]),
        modified_ns=int(signature["modified_ns"]),
    )
    return StoredUpload(
        path=storage.resolve_key(transfer.staging_key),
        key=transfer.staging_key,
        sha256=transfer.upload_sha256,
        size_bytes=file_signature.size,
        signature=file_signature,
    )


def _ensure_shadow_identity(transfer: OctxTransfer) -> tuple[str, str]:
    checkpoint = dict(transfer.checkpoint or {})
    id_namespace = checkpoint.get("id_namespace")
    source_config_id = checkpoint.get("source_config_id")
    if not isinstance(id_namespace, str) or not id_namespace:
        id_namespace = str(uuid.uuid4())
    if not isinstance(source_config_id, str) or not source_config_id:
        source_config_id = f"octx_{new_id()[:24]}"
    merge_checkpoint(
        transfer,
        id_namespace=id_namespace,
        source_config_id=source_config_id,
    )
    return id_namespace, source_config_id


def _import_started_at(transfer: OctxTransfer) -> datetime:
    checkpoint = dict(transfer.checkpoint or {})
    raw = checkpoint.get("import_started_at")
    try:
        started_at = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=UTC)
    except (TypeError, ValueError):
        started_at = datetime.now(UTC)
        merge_checkpoint(
            transfer,
            import_started_at=started_at.isoformat(),
        )
    return started_at


def _duration_seconds(started_at: datetime) -> int:
    return max(0, int((datetime.now(UTC) - started_at).total_seconds()))


def _promote_knowledge_documents(states: dict[str, Any], final_dir: str | Path) -> None:
    destination = Path(final_dir)
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    ready_states = [
        state for _, state in sorted(states.items()) if isinstance(state, dict) and state.get("status") == "ready"
    ]
    for position, state in enumerate(ready_states):
        source = Path(str(state["controlled_path"]))
        target = destination / f"{position:08d}.md"
        if source.resolve() != target.resolve():
            temporary = target.with_name(f".{target.name}.{new_id()}.tmp")
            shutil.copyfile(source, temporary)
            temporary.chmod(0o600)
            temporary.replace(target)
        elif not target.is_file():
            raise FileNotFoundError(f"promoted OCTX document is missing: {target}")
        state["controlled_path"] = str(target)


async def _ensure_transfer_active(session: AsyncSession, transfer: OctxTransfer, *, stage: str) -> None:
    await session.refresh(transfer, attribute_names=["cancellation_requested", "status"])
    if transfer.cancellation_requested or transfer.status is OctxTransferStatus.CANCELLED:
        raise ConflictError(
            f"OCTX transfer cancelled at {stage}",
            code=ErrorCode.OCTX_TRANSFER_CANCELLED,
            layer=ErrorLayer.API,
            stage=ErrorStage.OCTX_RESOLVE,
            retryable=False,
        )


def _export_document_state(documents: list[Document]) -> tuple[list[dict], list[dict]]:
    ready: list[dict] = []
    excluded: list[dict] = []
    for document in sorted(documents, key=lambda item: item.id):
        if document.status is DocumentStatus.READY and document.sag_source_id:
            ready.append(
                {
                    "id": document.id,
                    "article_id": str(document.sag_source_id),
                    "status": document.status.value,
                }
            )
            continue
        excluded.append(
            {
                "id": document.id,
                "filename": document.filename,
                "status": document.status.value,
                "error": str(document.error or "")[:500] or None,
            }
        )
    return ready, excluded


def _export_selection_fingerprint(ready: list[dict], excluded: list[dict], source_revision: int) -> str:
    encoded = json.dumps(
        {
            "ready": ready,
            "excluded": excluded,
            "source_revision": source_revision,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _export_checkpoint(
    *,
    ready: list[dict],
    excluded: list[dict],
    source_revision: int,
    selected_version: str,
    asset_name: str,
) -> dict[str, Any]:
    return {
        "asset_name": asset_name,
        "selected_version": selected_version,
        "selected_document_ids": [item["id"] for item in ready],
        "selected_article_ids": [item["article_id"] for item in ready],
        "excluded_documents": excluded,
        "source_revision": source_revision,
        "selection_fingerprint": _export_selection_fingerprint(ready, excluded, source_revision),
    }
