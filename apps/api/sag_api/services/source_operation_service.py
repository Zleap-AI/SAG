from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sag_api.core.error_taxonomy import ErrorLayer, ErrorStage
from sag_api.core.errors import ConflictError
from sag_api.db.base import new_id
from sag_api.db.models import OctxOperationLease, OctxSourceBinding


def _now() -> datetime:
    return datetime.now(UTC)


async def _acquire_one(
    session_factory: async_sessionmaker,
    resource_key: str,
    owner: str,
    expires_at: datetime,
) -> bool:
    async with session_factory() as session:
        claimed = await session.execute(
            update(OctxOperationLease)
            .where(
                OctxOperationLease.resource_key == resource_key,
                or_(
                    OctxOperationLease.owner_token == owner,
                    OctxOperationLease.expires_at <= _now(),
                ),
            )
            .values(owner_token=owner, expires_at=expires_at, heartbeat_at=_now())
        )
        if claimed.rowcount == 1:
            await session.commit()
            return True
        session.add(
            OctxOperationLease(
                resource_key=resource_key,
                owner_token=owner,
                expires_at=expires_at,
                heartbeat_at=_now(),
            )
        )
        try:
            await session.commit()
            return True
        except IntegrityError:
            await session.rollback()
            return False


async def _release_owned(session_factory: async_sessionmaker, resource_keys: Sequence[str], owner: str) -> None:
    if not resource_keys:
        return
    async with session_factory() as session:
        await session.execute(
            delete(OctxOperationLease).where(
                OctxOperationLease.resource_key.in_(resource_keys),
                OctxOperationLease.owner_token == owner,
            )
        )
        await session.commit()


async def _heartbeat(
    session_factory: async_sessionmaker,
    resource_keys: Sequence[str],
    owner: str,
    ttl_seconds: int,
    heartbeat_seconds: int,
    stop: asyncio.Event,
) -> None:
    while True:
        try:
            await asyncio.wait_for(stop.wait(), timeout=heartbeat_seconds)
            return
        except TimeoutError:
            pass
        now = _now()
        async with session_factory() as session:
            renewed = await session.execute(
                update(OctxOperationLease)
                .where(
                    OctxOperationLease.resource_key.in_(resource_keys),
                    OctxOperationLease.owner_token == owner,
                )
                .values(
                    expires_at=now + timedelta(seconds=ttl_seconds),
                    heartbeat_at=now,
                )
            )
            await session.commit()
            if renewed.rowcount != len(resource_keys):
                return


@asynccontextmanager
async def acquire_operation_lease(
    session_factory: async_sessionmaker,
    resource_keys: Sequence[str],
    owner: str,
    *,
    ttl_seconds: int = 60,
    heartbeat_seconds: int = 10,
) -> AsyncIterator[None]:
    keys = tuple(sorted(set(resource_keys)))
    if not keys or not owner:
        raise ValueError("OCTX operation lease requires resources and owner")
    if ttl_seconds <= 0 or heartbeat_seconds <= 0 or heartbeat_seconds >= ttl_seconds:
        raise ValueError("OCTX lease heartbeat must be positive and shorter than ttl")

    acquired: list[str] = []
    try:
        for key in keys:
            if not await _acquire_one(
                session_factory,
                key,
                owner,
                _now() + timedelta(seconds=ttl_seconds),
            ):
                raise ConflictError(
                    f"OCTX operation resource is busy: {key}",
                    layer=ErrorLayer.STORE,
                    stage=ErrorStage.OCTX_RESOLVE,
                    retryable=True,
                )
            acquired.append(key)

        stop = asyncio.Event()
        heartbeat = asyncio.create_task(
            _heartbeat(
                session_factory,
                keys,
                owner,
                ttl_seconds,
                heartbeat_seconds,
                stop,
            ),
            name=f"octx-lease-{owner}",
        )
        try:
            yield
        finally:
            stop.set()
            await heartbeat
    finally:
        await _release_owned(session_factory, acquired, owner)


@asynccontextmanager
async def source_content_mutation(
    session_factory: async_sessionmaker,
    source_id: str,
    operation: str,
    *,
    admission_timeout_seconds: float = 1,
) -> AsyncIterator[None]:
    """Serialize every source-content mutation with OCTX import/export jobs."""
    if not source_id or not operation:
        raise ValueError("source mutation requires source_id and operation")
    owner = f"{operation}:{new_id()}"
    deadline = time.monotonic() + admission_timeout_seconds
    while True:
        lease = acquire_operation_lease(session_factory, [f"source:{source_id}"], owner=owner)
        try:
            await lease.__aenter__()
            break
        except ConflictError:
            if time.monotonic() >= deadline:
                raise
            await asyncio.sleep(0.02)
    try:
        yield
    finally:
        await lease.__aexit__(None, None, None)


@asynccontextmanager
async def export_request_admission(
    session_factory: async_sessionmaker,
    source_id: str,
) -> AsyncIterator[None]:
    """Serialize export creation without blocking source-content mutations."""
    if not source_id:
        raise ValueError("export request admission requires source_id")
    async with acquire_operation_lease(
        session_factory,
        [f"octx-export-request:{source_id}"],
        owner=f"octx-export-request:{new_id()}",
    ):
        yield


async def _active_lease_owner(
    session_factory: async_sessionmaker,
    resource_key: str,
) -> str | None:
    async with session_factory() as session:
        return await session.scalar(
            select(OctxOperationLease.owner_token).where(
                OctxOperationLease.resource_key == resource_key,
                OctxOperationLease.expires_at > _now(),
            )
        )


@asynccontextmanager
async def source_upload_mutation(
    session_factory: async_sessionmaker,
    source_id: str,
    *,
    admission_timeout_seconds: float = 1,
    delete_wait_timeout_seconds: float = 60,
) -> AsyncIterator[None]:
    """Admit uploads, waiting only for the preceding document deletion.

    Document deletion can legitimately hold the source gate while engine-side
    records are removed. A user who immediately corrects a mistaken upload
    should not see that internal lease conflict. Other exclusive owners (OCTX
    import/export, source deletion, reprocessing) retain the normal fail-fast
    behavior so transfer snapshots cannot be mutated underneath them.
    """
    if not source_id:
        raise ValueError("source upload mutation requires source_id")
    owner = f"document-upload:{new_id()}"
    resource_key = f"source:{source_id}"
    started_at = time.monotonic()
    admission_deadline = started_at + admission_timeout_seconds
    delete_deadline = started_at + delete_wait_timeout_seconds
    while True:
        lease = acquire_operation_lease(session_factory, [resource_key], owner=owner)
        try:
            await lease.__aenter__()
            break
        except ConflictError as error:
            active_owner = await _active_lease_owner(session_factory, resource_key)
            deleting = bool(active_owner and active_owner.startswith("document-delete:"))
            now = time.monotonic()
            if deleting and now < delete_deadline:
                await asyncio.sleep(0.05)
                continue
            if now < admission_deadline:
                await asyncio.sleep(0.02)
                continue
            if deleting:
                raise ConflictError(
                    "上一份文档仍在清理，请稍后重试",
                    layer=ErrorLayer.STORE,
                    stage=ErrorStage.OCTX_RESOLVE,
                    retryable=True,
                ) from error
            raise
    try:
        yield
    finally:
        await lease.__aexit__(None, None, None)


def _processing_resource(source_id: str, job_id: str) -> str:
    return f"source:{source_id}:processor:{job_id}"


async def _wait_for_source_processors(
    session_factory: async_sessionmaker,
    source_ids: Sequence[str],
    *,
    timeout_seconds: float,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    while True:
        now = _now()
        async with session_factory() as session:
            active = await session.scalar(
                select(OctxOperationLease.resource_key)
                .where(
                    OctxOperationLease.expires_at > now,
                    or_(
                        *[
                            OctxOperationLease.resource_key.like(f"source:{source_id}:processor:%")
                            for source_id in source_ids
                        ]
                    ),
                )
                .limit(1)
            )
        if active is None:
            return
        if time.monotonic() >= deadline:
            raise ConflictError(
                f"source processors did not drain before timeout: {active}",
                layer=ErrorLayer.STORE,
                stage=ErrorStage.OCTX_RESOLVE,
                retryable=True,
            )
        await asyncio.sleep(0.05)


@asynccontextmanager
async def acquire_source_processing_lease(
    session_factory: async_sessionmaker,
    source_id: str,
    job_id: str,
    *,
    admission_timeout_seconds: float = 1,
) -> AsyncIterator[None]:
    """Register one shared processor while briefly crossing the source gate."""
    owner = f"document:{job_id}"
    processing = acquire_operation_lease(
        session_factory,
        [_processing_resource(source_id, job_id)],
        owner=owner,
    )
    deadline = time.monotonic() + admission_timeout_seconds
    while True:
        try:
            async with acquire_operation_lease(
                session_factory,
                [f"source:{source_id}"],
                owner=f"processor-admission:{job_id}",
            ):
                await processing.__aenter__()
            break
        except ConflictError:
            if time.monotonic() >= deadline:
                raise
            await asyncio.sleep(0.02)
    try:
        yield
    finally:
        await processing.__aexit__(None, None, None)


@asynccontextmanager
async def acquire_transfer_operation_lease(
    session_factory: async_sessionmaker,
    resource_keys: Sequence[str],
    owner: str,
    *,
    drain_source_ids: Sequence[str] = (),
    drain_timeout_seconds: float = 1800,
) -> AsyncIterator[None]:
    """Close source admission, drain registered processors, then run exclusively."""
    async with acquire_operation_lease(session_factory, resource_keys, owner=owner):
        if drain_source_ids:
            await _wait_for_source_processors(
                session_factory,
                tuple(sorted(set(drain_source_ids))),
                timeout_seconds=drain_timeout_seconds,
            )
        yield


@asynccontextmanager
async def acquire_source_exclusive_lease(
    session_factory: async_sessionmaker,
    source_id: str,
    owner: str,
    *,
    drain_timeout_seconds: float = 1800,
) -> AsyncIterator[None]:
    async with acquire_transfer_operation_lease(
        session_factory,
        [f"source:{source_id}"],
        owner,
        drain_source_ids=[source_id],
        drain_timeout_seconds=drain_timeout_seconds,
    ):
        yield


async def touch_source_revision(session: AsyncSession, source_id: str) -> int:
    binding = await session.scalar(select(OctxSourceBinding).where(OctxSourceBinding.source_id == source_id))
    if binding is None:
        return 0
    binding.content_revision += 1
    await session.flush()
    return binding.content_revision
