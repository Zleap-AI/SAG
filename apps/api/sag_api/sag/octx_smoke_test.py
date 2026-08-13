from __future__ import annotations

import asyncio
from typing import Any

from sqlalchemy import func, select

from sag_api.core.error_taxonomy import ErrorCode, ErrorLayer, ErrorStage
from sag_api.core.errors import ValidationError


async def smoke_test_installation(
    source_config_id: str,
    *,
    expected_counts: dict[str, int],
    engine_manager: Any,
    sag_session_factory: Any = None,
    sample_query: str = "hello",
    timeout_seconds: float = 30.0,
) -> dict[str, Any]:
    """Prove one shadow SAG partition is queryable end-to-end before activation.

    Checks three surfaces against the shadow ``source_config_id``:

    1. Relational counts match what the plan imported.
    2. One arbitrary chunk survives ``engine_manager.get_chunk`` read-back.
    3. A bounded vector search returns without raising.

    Any failure raises ``ValidationError(OCTX_SHADOW_VALIDATION_FAILED)`` so the
    caller can keep the installation in SHADOW and transition the transfer to
    FAILED — the atomic swap onto ``Source.sag_source_config_id`` is never taken.
    """
    from zleap.sag.db import get_session_factory
    from zleap.sag.db.models import (
        Entity,
        EventEntity,
        SourceChunk,
        SourceConfig,
        SourceEvent,
    )

    sessions = sag_session_factory or get_session_factory()

    def _fail(message: str) -> ValidationError:
        return ValidationError(
            message,
            code=ErrorCode.OCTX_SHADOW_VALIDATION_FAILED,
            layer=ErrorLayer.ENGINE,
            stage=ErrorStage.OCTX_INDEX,
            retryable=False,
        )

    async with sessions() as session:
        if await session.get(SourceConfig, source_config_id) is None:
            raise _fail(f"shadow SAG partition missing: {source_config_id}")
        actual = {
            "chunks": await session.scalar(
                select(func.count())
                .select_from(SourceChunk)
                .where(SourceChunk.source_config_id == source_config_id)
            ),
            "events": await session.scalar(
                select(func.count())
                .select_from(SourceEvent)
                .where(SourceEvent.source_config_id == source_config_id)
            ),
            "entities": await session.scalar(
                select(func.count())
                .select_from(Entity)
                .where(Entity.source_config_id == source_config_id)
            ),
            "event_entities": await session.scalar(
                select(func.count())
                .select_from(EventEntity)
                .join(SourceEvent, SourceEvent.id == EventEntity.event_id)
                .where(SourceEvent.source_config_id == source_config_id)
            ),
        }
        for kind, expected in expected_counts.items():
            got = int(actual.get(kind) or 0)
            if got != int(expected):
                raise _fail(
                    f"shadow SAG row count mismatch for {kind}: "
                    f"expected={expected} actual={got}"
                )

        sample_chunk_id = None
        if actual.get("chunks"):
            sample_chunk_id = await session.scalar(
                select(SourceChunk.id)
                .where(SourceChunk.source_config_id == source_config_id)
                .order_by(SourceChunk.id)
                .limit(1)
            )

    if sample_chunk_id is not None:
        try:
            chunk = await asyncio.wait_for(
                engine_manager.get_chunk(source_config_id, sample_chunk_id),
                timeout=timeout_seconds,
            )
        except TimeoutError as error:
            raise _fail("shadow chunk read-back timed out") from error
        except Exception as error:  # noqa: BLE001 - map any engine error into validation
            raise _fail(f"shadow chunk read-back failed: {error}") from error
        if chunk is None:
            raise _fail(
                f"shadow chunk read-back returned no data: {sample_chunk_id}"
            )

    try:
        outcome = await asyncio.wait_for(
            engine_manager.search(
                source_config_id,
                sample_query,
                strategy="vector",
                top_k=1,
            ),
            timeout=timeout_seconds,
        )
    except TimeoutError as error:
        raise _fail("shadow vector search timed out") from error
    except Exception as error:  # noqa: BLE001 - map any engine error into validation
        raise _fail(f"shadow vector search failed: {error}") from error

    return {
        "counts": {kind: int(value or 0) for kind, value in actual.items()},
        "sample_chunk_id": sample_chunk_id,
        "search_stats": dict(getattr(outcome, "stats", {}) or {}),
    }
