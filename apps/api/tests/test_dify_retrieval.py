"""Dify 外部知识库兼容检索的 HTTP 契约。"""

from __future__ import annotations

import uuid

import httpx
import pytest


def _set_dify_key(monkeypatch, settings, value: str | None) -> None:
    """Allow the contract tests to describe the setting before it exists."""

    monkeypatch.setitem(settings.__dict__, "dify_api_key", value)


@pytest.mark.asyncio
async def test_dify_retrieval_maps_one_source_to_traceable_records(monkeypatch):
    from sag_api.core.config import settings
    from sag_api.core.deps import get_engine_manager
    from sag_api.main import app
    from sag_api.sag.dto import RetrievedSection, SearchOutcome

    class RecordingEngine:
        strategy: str | None = None
        top_k: int | None = None
        source_id: str | None = None

        async def provision(self, *_args):
            return None

        async def search_many(self, targets, query, *, strategy=None, top_k=None):
            assert len(targets) == 1
            source_config_id, source = targets[0]
            self.strategy = strategy
            self.top_k = top_k
            self.source_id = source.id
            return SearchOutcome(
                query=query,
                sections=[
                    RetrievedSection(
                        chunk_id="chunk-123",
                        heading="关键章节",
                        content="星河计划的内部识别码是 SAG-TRACE-7291。",
                        score=0.91,
                        source_config_id=source_config_id,
                    ),
                    RetrievedSection(
                        chunk_id="chunk-low",
                        heading="低分章节",
                        content="这条记录应被 Dify 的阈值过滤。",
                        score=0.55,
                        source_config_id=source_config_id,
                    ),
                ],
            )

    engine = RecordingEngine()
    _set_dify_key(monkeypatch, settings, "dify-secret")
    app.dependency_overrides[get_engine_manager] = lambda: engine
    try:
        transport = httpx.ASGITransport(app=app)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
                registered = await client.post(
                    "/api/v1/auth/register",
                    json={
                        "email": f"dify-{uuid.uuid4().hex}@t.com",
                        "password": "password123",
                    },
                )
                assert registered.status_code == 201, registered.text
                user_headers = {
                    "Authorization": f"Bearer {registered.json()['access_token']}"
                }
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
                        "query": "星河计划的识别码是什么",
                        "retrieval_setting": {
                            "top_k": 3,
                            "score_threshold": 0.6,
                        },
                    },
                )

        assert response.status_code == 200, response.text
        assert engine.strategy == "multi"
        assert engine.top_k == 11
        assert engine.source_id == source.json()["id"]
        assert response.json() == {
            "records": [
                {
                    "content": "星河计划的内部识别码是 SAG-TRACE-7291。",
                    "title": "Dify 接入测试源 — 关键章节",
                    "score": 0.655,
                    "metadata": {
                        "document_id": f"{source.json()['id']}:chunk-123",
                        "source_id": source.json()["id"],
                        "source_name": "Dify 接入测试源",
                        "chunk_id": "chunk-123",
                        "heading": "关键章节",
                    },
                }
            ]
        }
    finally:
        app.dependency_overrides.pop(get_engine_manager, None)


@pytest.mark.asyncio
async def test_dify_retrieval_handles_auth_probe_and_invalid_requests(
    monkeypatch,
):
    from sag_api.core.config import settings
    from sag_api.main import app

    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            _set_dify_key(monkeypatch, settings, None)
            disabled = await client.post("/api/v1/dify/retrieval", json={})
            assert disabled.status_code == 503

            _set_dify_key(monkeypatch, settings, "dify-secret")
            missing = await client.post("/api/v1/dify/retrieval", json={})
            assert missing.status_code == 403
            wrong = await client.post(
                "/api/v1/dify/retrieval",
                headers={"Authorization": "Bearer wrong"},
                json={},
            )
            assert wrong.status_code == 403

            probe = await client.post(
                "/api/v1/dify/retrieval",
                headers={"Authorization": "Bearer dify-secret"},
                json={
                    "knowledge_id": "",
                    "query": "",
                    "retrieval_setting": {"top_k": 1, "score_threshold": 0},
                },
            )
            assert probe.status_code == 200
            assert probe.json() == {"records": []}

            partial = await client.post(
                "/api/v1/dify/retrieval",
                headers={"Authorization": "Bearer dify-secret"},
                json={"knowledge_id": "source-id", "query": ""},
            )
            assert partial.status_code == 422
            assert partial.json()["error"]["code"] == "validation_error"

            metadata_filter = await client.post(
                "/api/v1/dify/retrieval",
                headers={"Authorization": "Bearer dify-secret"},
                json={
                    "knowledge_id": "source-id",
                    "query": "metadata",
                    "metadata_condition": {
                        "logical_operator": "and",
                        "conditions": [],
                    },
                },
            )
            assert metadata_filter.status_code == 422
            assert metadata_filter.json()["error"]["code"] == "validation_error"

            invalid_settings = await client.post(
                "/api/v1/dify/retrieval",
                headers={"Authorization": "Bearer dify-secret"},
                json={
                    "knowledge_id": "source-id",
                    "query": "invalid",
                    "retrieval_setting": {
                        "top_k": 0,
                        "score_threshold": 1.1,
                    },
                },
            )
            assert invalid_settings.status_code == 422

            unknown_source = await client.post(
                "/api/v1/dify/retrieval",
                headers={"Authorization": "Bearer dify-secret"},
                json={"knowledge_id": uuid.uuid4().hex, "query": "unknown"},
            )
            assert unknown_source.status_code == 404
