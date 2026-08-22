from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from sqlalchemy import select, update

from sag_api.db.models import Document, Job, Source
from sag_api.enums import DocumentStatus, JobStatus, JobType
from sag_api.upgrades.types import StorageUpgradeError
from sag_api.upgrades.zleap_sag_0_7_to_0_8.relational import GenerationIdentity

CheckpointAction = Literal["enrich", "restart", "block"]


@dataclass(frozen=True)
class CheckpointUpdate:
    job_id: str
    document_id: str
    action: CheckpointAction
    payload: dict[str, Any]
    message: str | None = None


@dataclass(frozen=True)
class CheckpointMigrationPlan:
    updates: tuple[CheckpointUpdate, ...]


@dataclass(frozen=True)
class CheckpointMigrationReport:
    applied: int
    skipped: int


async def plan_checkpoint_updates(
    session_factory: Any,
    generation_by_source: dict[tuple[str, str], GenerationIdentity],
) -> CheckpointMigrationPlan:
    updates: list[CheckpointUpdate] = []
    async with session_factory() as session:
        result = await session.execute(
            select(Job, Document, Source)
            .join(Document, Job.document_id == Document.id)
            .join(Source, Document.source_id == Source.id)
            .where(
                Job.type == JobType.PROCESS_DOCUMENT,
                Job.status == JobStatus.PAUSED,
                Document.status == DocumentStatus.PAUSED,
            )
        )
        for job, document, source in result.all():
            payload = dict(job.payload or {})
            checkpoint = payload.get("process_checkpoint")
            if not isinstance(checkpoint, dict) or not checkpoint.get("chunk_ids"):
                continue
            if checkpoint.get("generation_id") and checkpoint.get("chunk_version"):
                continue
            source_id = str(checkpoint.get("source_id") or document.sag_source_id or "")
            identity = generation_by_source.get((str(source.sag_source_config_id), source_id))
            if identity is not None:
                payload["process_checkpoint"] = {
                    **checkpoint,
                    "generation_id": identity.generation_id,
                    "source_version": identity.source_version,
                    "chunk_version": identity.chunk_version,
                }
                action: CheckpointAction = "enrich"
                message = None
            elif Path(document.storage_path).is_file():
                payload.pop("process_checkpoint", None)
                payload.pop("resume_requested", None)
                payload.pop("pause_requested", None)
                action = "restart"
                message = None
            else:
                action = "block"
                message = "旧版抽取断点无法映射，且原文件已不存在；请重新上传原文件"
            updates.append(
                CheckpointUpdate(
                    job_id=str(job.id),
                    document_id=str(document.id),
                    action=action,
                    payload=payload,
                    message=message,
                )
            )
    return CheckpointMigrationPlan(tuple(updates))


async def apply_checkpoint_plan(session_factory: Any, plan: CheckpointMigrationPlan) -> CheckpointMigrationReport:
    applied = 0
    skipped = 0
    async with session_factory() as session, session.begin():
        for item in plan.updates:
            job_values: dict[str, Any] = {"payload": item.payload}
            document_values: dict[str, Any] = {}
            if item.action == "restart":
                job_values.update(
                    status=JobStatus.QUEUED,
                    started_at=None,
                    finished_at=None,
                    error=None,
                )
                document_values.update(
                    status=DocumentStatus.PENDING,
                    error=None,
                    error_layer=None,
                    error_stage=None,
                    progress=0,
                )
            elif item.action == "block":
                document_values.update(
                    error=item.message,
                    error_layer="store",
                    error_stage="persist",
                )

            job_result = await session.execute(
                update(Job)
                .where(Job.id == item.job_id, Job.status == JobStatus.PAUSED)
                .values(**job_values)
                .execution_options(synchronize_session=False)
            )
            if job_result.rowcount != 1:
                skipped += 1
                continue
            if document_values:
                document_result = await session.execute(
                    update(Document)
                    .where(
                        Document.id == item.document_id,
                        Document.status == DocumentStatus.PAUSED,
                    )
                    .values(**document_values)
                    .execution_options(synchronize_session=False)
                )
                if document_result.rowcount != 1:
                    raise StorageUpgradeError(
                        f"document checkpoint changed concurrently: {item.document_id}",
                        stage="checkpoints",
                        recoverable=True,
                    )
            applied += 1
    return CheckpointMigrationReport(applied=applied, skipped=skipped)
