from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from sqlalchemy import select

from sag_api.core.logging import get_logger

VectorFieldFetcher = Callable[..., Awaitable[dict[str, dict[str, Any]]]]

log = get_logger("sag.vectors")


def _chunk_document(
    chunk: Any,
    source_config_id: str,
    *,
    heading_vector: Any,
    content_vector: Any,
) -> dict[str, Any]:
    return {
        "id": str(chunk.id),
        "chunk_id": str(chunk.id),
        "source_id": str(chunk.source_id),
        "source_config_id": source_config_id,
        "rank": chunk.rank,
        "heading": chunk.heading,
        "content": chunk.content,
        "heading_vector": heading_vector,
        "content_vector": content_vector,
        "references": chunk.references or [],
        "chunk_type": "TEXT",
        "content_length": chunk.chunk_length,
    }


async def _rebuild_chunk_vectors(
    chunks: Sequence[Any],
    source_config_id: str,
    *,
    embedding_client: Any,
    vector_store: Any,
) -> int:
    """重建缺失的内容与标题向量，让 load 阶段被吞掉的索引失败自愈。

    依赖方 loader 对批量索引的部分失败只记日志不抛错，可能出现 chunk 行
    已落库但向量记录缺失（或内容向量为空）。chunk 行是最权威的数据源，
    这里直接用其内容与标题重新生成两组向量并回写，而不是让整篇文档失败。
    """
    content_inputs = [str(chunk.content or chunk.heading or "") for chunk in chunks]
    if any(not value for value in content_inputs):
        raise RuntimeError("cannot rebuild vectors for an empty chunk")
    content_vectors = await embedding_client.batch_generate(content_inputs)
    if len(content_vectors) != len(chunks):
        raise RuntimeError(
            "chunk content embedding batch size mismatch: "
            f"expected={len(chunks)} actual={len(content_vectors)}"
        )

    heading_inputs = [str(chunk.heading or chunk.content or "") for chunk in chunks]
    heading_vectors = await embedding_client.batch_generate(heading_inputs)
    if len(heading_vectors) != len(chunks):
        raise RuntimeError(
            "chunk heading embedding batch size mismatch: "
            f"expected={len(chunks)} actual={len(heading_vectors)}"
        )

    documents = [
        _chunk_document(
            chunk,
            source_config_id,
            heading_vector=heading_vector,
            content_vector=content_vector,
        )
        for chunk, heading_vector, content_vector in zip(
            chunks, heading_vectors, content_vectors, strict=True
        )
    ]
    result = await vector_store.bulk_index(
        index="source_chunks",
        documents=documents,
        return_details=True,
        routing=source_config_id,
    )
    if (
        not isinstance(result, dict)
        or int(result.get("error_count", 0))
        or int(result.get("success_count", 0)) != len(documents)
    ):
        raise RuntimeError(f"chunk vector rebuild failed: {result!r}")
    log.warning(
        "已重建缺失的切片向量 source_config_id=%s count=%d",
        source_config_id,
        len(documents),
    )
    return len(documents)


async def complete_chunk_heading_vectors(
    chunks: Sequence[Any],
    source_config_id: str,
    *,
    embedding_client: Any,
    vector_store: Any,
    fetch_vector_fields: VectorFieldFetcher,
    batch_size: int = 100,
) -> int:
    """Fill only missing chunk heading vectors, using content as the fallback input."""
    if batch_size < 1:
        raise ValueError("chunk heading vector batch size must be positive")

    completed = 0
    for offset in range(0, len(chunks), batch_size):
        batch = list(chunks[offset : offset + batch_size])
        record_ids = [str(chunk.id) for chunk in batch]
        stored = await fetch_vector_fields(
            vector_store,
            "source_chunks",
            record_ids,
            ["heading_vector", "content_vector"],
            routing=source_config_id,
        )
        missing: list[tuple[Any, dict[str, Any]]] = []
        rebuild: list[Any] = []
        for chunk in batch:
            fields = stored.get(str(chunk.id))
            if fields is None or fields.get("content_vector") is None:
                # 向量记录缺失或内容向量为空：load 阶段索引失败被依赖方吞掉，
                # 直接基于 chunk 行重建内容与标题向量，避免整篇文档失败。
                rebuild.append(chunk)
                continue
            if fields.get("heading_vector") is not None:
                continue
            missing.append((chunk, fields))

        if rebuild:
            completed += await _rebuild_chunk_vectors(
                rebuild,
                source_config_id,
                embedding_client=embedding_client,
                vector_store=vector_store,
            )

        if not missing:
            continue

        inputs = [str(chunk.heading or chunk.content or "") for chunk, _ in missing]
        if any(not value for value in inputs):
            raise RuntimeError("cannot generate a heading vector for an empty chunk")
        heading_vectors = await embedding_client.batch_generate(inputs)
        if len(heading_vectors) != len(missing):
            raise RuntimeError(
                "chunk heading embedding batch size mismatch: "
                f"expected={len(missing)} actual={len(heading_vectors)}"
            )

        documents = [
            _chunk_document(
                chunk,
                source_config_id,
                heading_vector=heading_vector,
                content_vector=fields["content_vector"],
            )
            for (chunk, fields), heading_vector in zip(missing, heading_vectors, strict=True)
        ]
        result = await vector_store.bulk_index(
            index="source_chunks",
            documents=documents,
            return_details=True,
            routing=source_config_id,
        )
        if (
            not isinstance(result, dict)
            or int(result.get("error_count", 0))
            or int(result.get("success_count", 0)) != len(documents)
        ):
            raise RuntimeError(f"chunk heading vector completion failed: {result!r}")
        completed += len(documents)
    return completed


async def complete_loaded_chunk_heading_vectors(
    chunk_ids: Sequence[str],
    source_config_id: str,
) -> int:
    """Complete heading vectors for chunks created by one document load."""
    if not chunk_ids:
        return 0

    from zleap.sag.core.ai.factory import get_embedding_client
    from zleap.sag.core.storage.client import get_vector_client
    from zleap.sag.db import SourceChunk, get_session_factory

    from sag_api.sag.octx_vector_protocol import _fetch_vector_fields

    sessions = get_session_factory()
    async with sessions() as session:
        chunks = (
            (
                await session.execute(
                    select(SourceChunk)
                    .where(
                        SourceChunk.source_config_id == source_config_id,
                        SourceChunk.id.in_(list(chunk_ids)),
                    )
                    .order_by(SourceChunk.id)
                )
            )
            .scalars()
            .all()
        )
    if len(chunks) != len(set(chunk_ids)):
        raise RuntimeError(
            "loaded chunk rows are incomplete: "
            f"expected={len(set(chunk_ids))} actual={len(chunks)}"
        )

    embedding_client = await get_embedding_client(scenario="general")
    return await complete_chunk_heading_vectors(
        chunks,
        source_config_id,
        embedding_client=embedding_client,
        vector_store=get_vector_client(),
        fetch_vector_fields=_fetch_vector_fields,
    )
