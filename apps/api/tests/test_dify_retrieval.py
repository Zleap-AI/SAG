"""Dify 外部知识库兼容检索的 HTTP 契约。"""

from __future__ import annotations

import uuid

import httpx
import pytest


def test_dify_retrieval_defaults_to_vector_strategy():
    from sag_api.core.config import settings

    assert settings.dify_search_strategy == "vector"


@pytest.mark.asyncio
async def test_dify_retrieval_returns_traceable_records_from_the_requested_source(monkeypatch):
    from sag_api.core.config import settings
    from sag_api.core.deps import get_engine_manager
    from sag_api.main import app
    from sag_api.sag.dto import RetrievedSection, SearchOutcome

    class RecordingEngine:
        strategy: str | None = None
        top_k: int | None = None

        async def provision(self, *_args):
            return None

        async def search_many(self, targets, query, *, strategy=None, top_k=None):
            self.strategy = strategy
            self.top_k = top_k
            source_config_id = targets[0][0]
            return SearchOutcome(
                query=query,
                sections=[
                    RetrievedSection(
                        chunk_id="chunk-123",
                        heading="关键章节",
                        content="可由 Dify 用作回答证据的原文。",
                        score=0.91,
                        source_config_id=source_config_id,
                    )
                ],
                stats={"effective_strategy": "multi"},
            )

    engine = RecordingEngine()
    monkeypatch.setattr(settings, "dify_api_key", "dify-secret")
    monkeypatch.setattr(settings, "dify_search_strategy", "vector", raising=False)
    app.dependency_overrides[get_engine_manager] = lambda: engine
    try:
        transport = httpx.ASGITransport(app=app)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
                registered = await client.post(
                    "/api/v1/auth/register",
                    json={"email": f"dify-{uuid.uuid4().hex}@t.com", "password": "password123"},
                )
                assert registered.status_code == 201, registered.text
                user_headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}
                source = await client.post(
                    "/api/v1/sources",
                    headers=user_headers,
                    json={"name": "Dify 接入测试源"},
                )
                assert source.status_code == 201, source.text

                response = await client.post(
                    "/api/v1/dify/retrieval",
                    headers={"Authorization": "Bearer dify-secret"},
                    json={
                        "knowledge_id": source.json()["id"],
                        "query": "Dify 应该用什么作为证据？",
                        "retrieval_setting": {"top_k": 3, "score_threshold": 0.6},
                    },
                )

        assert response.status_code == 200, response.text
        assert engine.strategy == "vector"
        assert engine.top_k == 11
        records = response.json()["records"]
        assert len(records) == 1
        assert records[0]["content"] == "可由 Dify 用作回答证据的原文。"
        assert records[0]["title"] == "Dify 接入测试源 — 关键章节"
        assert records[0]["score"] > 0.6
        assert records[0]["metadata"] == {
            "document_id": f"{source.json()['id']}:chunk-123",
            "source_id": source.json()["id"],
            "source_name": "Dify 接入测试源",
            "chunk_id": "chunk-123",
            "heading": "关键章节",
            "retrieval_strategy": "multi",
            "fallback_used": False,
        }
    finally:
        app.dependency_overrides.pop(get_engine_manager, None)


@pytest.mark.asyncio
async def test_dify_retrieval_handles_probe_authentication_and_invalid_requests(monkeypatch):
    from sag_api.core.config import settings
    from sag_api.main import app

    monkeypatch.setattr(settings, "dify_api_key", "dify-secret")
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            denied = await client.post("/api/v1/dify/retrieval", json={})
            assert denied.status_code == 403

            probe = await client.post(
                "/api/v1/dify/retrieval",
                headers={"Authorization": "Bearer dify-secret"},
                json={"knowledge_id": "", "query": "", "retrieval_setting": {"top_k": 1}},
            )
            assert probe.status_code == 200
            assert probe.json() == {"records": []}

            partial = await client.post(
                "/api/v1/dify/retrieval",
                headers={"Authorization": "Bearer dify-secret"},
                json={"knowledge_id": "a" * 32, "query": ""},
            )
            assert partial.status_code == 422
            assert partial.json()["error"]["code"] == "validation_error"

            filtered = await client.post(
                "/api/v1/dify/retrieval",
                headers={"Authorization": "Bearer dify-secret"},
                json={
                    "knowledge_id": "a" * 32,
                    "query": "metadata filter",
                    "metadata_condition": {"logical_operator": "and", "conditions": []},
                },
            )
            assert filtered.status_code == 422
            assert filtered.json()["error"]["code"] == "validation_error"


@pytest.mark.asyncio
async def test_sag_search_records_when_multi_falls_back_to_vector(monkeypatch):
    from sag_api.core.config import settings
    from sag_api.sag.dto import RetrievedSection, SearchOutcome
    from sag_api.sag.engine_manager import EngineManager

    manager = EngineManager(settings)

    async def search_raw(_source_config_id, _query, *, source, strategy, top_k):
        if strategy == "multi":
            return SearchOutcome(query="fallback", sections=[], stats={})
        return SearchOutcome(
            query="fallback",
            sections=[RetrievedSection(content="vector evidence", score=0.8)],
            stats={"upstream": "vector"},
        )

    monkeypatch.setattr(manager, "_search_raw", search_raw)

    result = await manager.search("source-1", "fallback", strategy="multi", top_k=2)

    assert result.sections[0].content == "vector evidence"
    assert result.stats["requested_strategy"] == "multi"
    assert result.stats["effective_strategy"] == "vector"
    assert result.stats["fallback_used"] is True


@pytest.mark.asyncio
async def test_sag_search_many_preserves_single_source_fallback_metadata(monkeypatch):
    from sag_api.core.config import settings
    from sag_api.sag.dto import RetrievedSection, SearchOutcome
    from sag_api.sag.engine_manager import EngineManager

    manager = EngineManager(settings)

    async def search(_source_config_id, _query, **_kwargs):
        return SearchOutcome(
            query="fallback",
            sections=[RetrievedSection(content="vector evidence", score=0.8)],
            stats={
                "requested_strategy": "multi",
                "effective_strategy": "vector",
                "fallback_used": True,
            },
        )

    monkeypatch.setattr(manager, "search", search)

    result = await manager.search_many([("source-1", None)], "fallback", strategy="multi", top_k=2)

    assert result.stats["requested_strategy"] == "multi"
    assert result.stats["effective_strategy"] == "vector"
    assert result.stats["fallback_used"] is True
