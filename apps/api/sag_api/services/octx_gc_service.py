from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from sag_api.core.config import settings
from sag_api.core.errors import ConflictError, NotFoundError
from sag_api.db.models import Document, OctxInstallation, OctxTransfer, Source
from sag_api.db.models.octx import transition_installation
from sag_api.enums import OctxInstallationStatus, OctxTransferStatus
from sag_api.octx.storage import OctxStorage

_VECTOR_INDEXES = (
    "source_chunks",
    "event_vectors",
    "event_entity_vectors",
    "entity_vectors",
)
_TERMINAL_TRANSFER_STATUSES = {
    OctxTransferStatus.READY,
    OctxTransferStatus.FAILED,
    OctxTransferStatus.CANCELLED,
    OctxTransferStatus.EXPIRED,
}


def _literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


async def _delete_vector_partition(
    client: Any, index: str, source_config_id: str
) -> None:
    open_table = getattr(client, "_open_table", None)
    if callable(open_table):
        table = await open_table(index)
        if table is not None:
            await table.delete(f"source_config_id = {_literal(source_config_id)}")
        return

    raw_client = getattr(client, "client", client)
    delete_by_query = getattr(raw_client, "delete_by_query", None)
    if callable(delete_by_query):
        await delete_by_query(
            index=index,
            query={"term": {"source_config_id": source_config_id}},
            conflicts="proceed",
            refresh=True,
        )
        return

    engine_factory = getattr(client, "_engine", None)
    if callable(engine_factory):
        quote = "`" if client.__class__.__name__ == "OceanBaseVectorStore" else '"'
        statement = text(
            f"DELETE FROM {quote}{index}{quote} "
            "WHERE source_config_id = :source_config_id"
        )
        async with engine_factory().begin() as connection:
            await connection.execute(
                statement, {"source_config_id": source_config_id}
            )
        return

    raise RuntimeError(
        f"vector backend cannot delete OCTX partition from index {index}"
    )


async def delete_source_partition(
    source_config_id: str,
    *,
    sag_session_factory: Any = None,
    vector_client: Any = None,
) -> None:
    """Idempotently delete one exact zleap-sag relational/vector partition."""
    if sag_session_factory is None:
        from zleap.sag.db import get_session_factory

        sag_session_factory = get_session_factory()
    if vector_client is None:
        from zleap.sag.core.storage.client import get_vector_client

        vector_client = get_vector_client()

    for index in _VECTOR_INDEXES:
        await _delete_vector_partition(vector_client, index, source_config_id)

    from zleap.sag.db.models import SourceConfig

    async with sag_session_factory() as sag_session:
        await sag_session.execute(
            delete(SourceConfig).where(SourceConfig.id == source_config_id)
        )
        await sag_session.commit()


def _controlled_document_path(
    raw_path: str, *, upload_root: Path, source_id: str
) -> Path | None:
    root = (upload_root / source_id).resolve()
    path = Path(raw_path).resolve()
    if path == root or root not in path.parents:
        return None
    return path


async def gc_installation(
    session: AsyncSession,
    installation_id: str,
    *,
    sag_session_factory: Any = None,
    vector_client: Any = None,
    engine_manager: Any = None,
    upload_root: Path | None = None,
) -> dict[str, int]:
    """Collect an expired retained/failed installation, never the live partition."""
    installation = await session.get(OctxInstallation, installation_id)
    if installation is None:
        raise NotFoundError("OCTX installation not found")
    if installation.status not in {
        OctxInstallationStatus.RETAINED,
        OctxInstallationStatus.FAILED,
    }:
        raise ConflictError("only retained or failed OCTX installations can be collected")
    source = await session.get(Source, installation.source_id)
    if source is not None and source.sag_source_config_id == installation.sag_source_config_id:
        raise ConflictError("current source partition cannot be collected")
    now = datetime.now(UTC)
    if (
        installation.status is OctxInstallationStatus.RETAINED
        and (installation.retain_until is None or installation.retain_until > now)
    ):
        raise ConflictError("OCTX installation rollback retention has not expired")

    documents = (
        (
            await session.execute(
                select(Document).where(
                    Document.octx_installation_id == installation.id,
                    Document.is_active.is_(False),
                )
            )
        )
        .scalars()
        .all()
    )
    if await session.scalar(
        select(Document.id).where(
            Document.octx_installation_id == installation.id,
            Document.is_active.is_(True),
        ).limit(1)
    ):
        raise ConflictError("installation still owns active documents")

    await delete_source_partition(
        installation.sag_source_config_id,
        sag_session_factory=sag_session_factory,
        vector_client=vector_client,
    )
    if engine_manager is not None:
        await engine_manager.release(installation.sag_source_config_id)

    controlled_root = Path(upload_root or settings.upload_dir)
    for document in documents:
        controlled = _controlled_document_path(
            document.storage_path,
            upload_root=controlled_root,
            source_id=installation.source_id,
        )
        if controlled is not None:
            controlled.unlink(missing_ok=True)
        await session.delete(document)
    transition_installation(installation, OctxInstallationStatus.GC)
    await session.commit()
    return {"documents": len(documents), "partitions": 1}


async def gc_expired_transfers(
    session: AsyncSession,
    *,
    storage: OctxStorage,
) -> dict[str, int]:
    """Remove staging only for terminal transfers whose explicit TTL elapsed."""
    now = datetime.now(UTC)
    transfers = (
        (
            await session.execute(
                select(OctxTransfer).where(
                    OctxTransfer.status.in_(_TERMINAL_TRANSFER_STATUSES),
                    OctxTransfer.expires_at.is_not(None),
                    OctxTransfer.expires_at <= now,
                )
            )
        )
        .scalars()
        .all()
    )
    removed = 0
    for transfer in transfers:
        directory = storage.staging_dir(transfer.id)
        if directory.exists():
            shutil.rmtree(directory)
            removed += 1
        transfer.staging_key = None
        transfer.input_signature = None
    await session.commit()
    return {"transfers": removed}
