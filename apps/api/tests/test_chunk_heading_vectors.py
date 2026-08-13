from __future__ import annotations

from types import SimpleNamespace

import pytest


@pytest.mark.asyncio
async def test_complete_chunk_heading_vectors_falls_back_to_content_without_changing_heading():
    from sag_api.sag.chunk_heading_vectors import complete_chunk_heading_vectors

    chunk = SimpleNamespace(
        id="chunk-1",
        source_id="article-1",
        source_config_id="source-config-1",
        rank=0,
        heading=None,
        content="正文内容",
        references=[],
        chunk_length=4,
    )

    class EmbeddingClient:
        def __init__(self) -> None:
            self.inputs: list[list[str]] = []

        async def batch_generate(self, texts: list[str]) -> list[list[float]]:
            self.inputs.append(texts)
            return [[0.1, 0.2] for _ in texts]

    class VectorStore:
        def __init__(self) -> None:
            self.documents: list[dict] = []

        async def bulk_index(self, *, index, documents, return_details, routing):
            assert index == "source_chunks"
            assert return_details is True
            assert routing == "source-config-1"
            self.documents.extend(documents)
            return {
                "success": True,
                "success_count": len(documents),
                "error_count": 0,
                "errors": [],
            }

    embedding = EmbeddingClient()
    vector_store = VectorStore()

    async def fetch_fields(_store, _index, record_ids, fields, *, routing):
        assert record_ids == ["chunk-1"]
        assert fields == ["heading_vector", "content_vector"]
        assert routing == "source-config-1"
        return {
            "chunk-1": {
                "heading_vector": None,
                "content_vector": [0.3, 0.4],
            }
        }

    completed = await complete_chunk_heading_vectors(
        [chunk],
        "source-config-1",
        embedding_client=embedding,
        vector_store=vector_store,
        fetch_vector_fields=fetch_fields,
    )

    assert completed == 1
    assert embedding.inputs == [["正文内容"]]
    assert vector_store.documents == [
        {
            "id": "chunk-1",
            "chunk_id": "chunk-1",
            "source_id": "article-1",
            "source_config_id": "source-config-1",
            "rank": 0,
            "heading": None,
            "content": "正文内容",
            "heading_vector": [0.1, 0.2],
            "content_vector": [0.3, 0.4],
            "references": [],
            "chunk_type": "TEXT",
            "content_length": 4,
        }
    ]


@pytest.mark.asyncio
async def test_complete_chunk_heading_vectors_does_not_regenerate_existing_vectors():
    from sag_api.sag.chunk_heading_vectors import complete_chunk_heading_vectors

    chunk = SimpleNamespace(id="chunk-1")

    class EmbeddingClient:
        async def batch_generate(self, _texts):
            raise AssertionError("existing heading vectors must be reused")

    class VectorStore:
        async def bulk_index(self, **_kwargs):
            raise AssertionError("unchanged chunks must not be rewritten")

    async def fetch_fields(_store, _index, _record_ids, _fields, *, routing):
        assert routing == "source-config-1"
        return {
            "chunk-1": {
                "heading_vector": [0.1, 0.2],
                "content_vector": [0.3, 0.4],
            }
        }

    completed = await complete_chunk_heading_vectors(
        [chunk],
        "source-config-1",
        embedding_client=EmbeddingClient(),
        vector_store=VectorStore(),
        fetch_vector_fields=fetch_fields,
    )

    assert completed == 0
