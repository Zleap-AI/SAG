"""全局搜索只公开快速/精确两档，并始终保持信源 fan-out 边界。"""

import asyncio
import time
import uuid
from contextlib import asynccontextmanager

import httpx
import pytest
from sqlalchemy import delete


async def _register(client: httpx.AsyncClient) -> dict[str, str]:
    response = await client.post(
        "/api/v1/auth/register",
        json={"email": "search-strategy@t.com", "password": "password123"},
    )
    assert response.status_code == 201, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


@pytest.mark.asyncio
async def test_global_search_forwards_validated_strategy():
    from sag_api.core.deps import get_engine_manager
    from sag_api.main import app
    from sag_api.sag.dto import (
        EntityInfo,
        GraphAssociationInfo,
        GraphEventInfo,
        RetrievedSection,
        SearchOutcome,
        SourceGraphInfo,
    )

    class RecordingEngine:
        strategy: str | None = None
        top_k: int | None = None
        event_top_k: int | None = None

        def __init__(self):
            self.started: set[str] = set()
            self.parallel_gate = asyncio.Event()

        async def _meet_parallel_gate(self, channel: str) -> None:
            self.started.add(channel)
            if len(self.started) == 2:
                self.parallel_gate.set()
            await asyncio.wait_for(self.parallel_gate.wait(), timeout=1)

        async def provision(self, *_args):
            return None

        async def search_many(self, targets, query, *, strategy=None, top_k=None):
            await self._meet_parallel_gate("chunks")
            self.strategy = strategy
            self.top_k = top_k
            source_config_id = targets[0][0]
            return SearchOutcome(
                query=query,
                sections=[
                    RetrievedSection(
                        chunk_id="chunk-1",
                        heading="原始分块标题",
                        content="原始分块正文",
                        score=0.82,
                        source_config_id=source_config_id,
                    )
                ],
                stats={"strategy": strategy},
            )

        async def search_event_scores(self, query, sources_by_config, *, limit=None):
            await self._meet_parallel_gate("events")
            self.event_top_k = limit
            source_config_id = next(iter(sources_by_config))
            # The directly recalled event belongs to another chunk. This is the
            # sparse-event case that chunk-only graph mapping used to lose.
            return {(source_config_id, "event-1"): 0.94}

        async def graph_for_sections(self, sections, sources_by_config, **kwargs):
            source_config_id = sections[0].source_config_id
            assert kwargs["event_scores"] == {(source_config_id, "event-1"): 0.94}
            return SourceGraphInfo(
                events=[
                    GraphEventInfo(
                        id="event-1",
                        source_config_id=source_config_id,
                        source_id="document-1",
                        chunk_id="event-chunk-not-in-section-results",
                        title="外卖骑手收入变化",
                        summary="报告分析了工作时长、技能与收入之间的关系。",
                        category="劳动研究",
                        score=0.94,
                    )
                ],
                entities=[
                    EntityInfo(
                        id="entity-1",
                        name="外卖骑手",
                        type="职业",
                        description="平台配送劳动者",
                        heat=1,
                    )
                ],
                associations=[
                    GraphAssociationInfo(event_id="event-1", entity_id="entity-1")
                ],
            )

    engine = RecordingEngine()
    app.dependency_overrides[get_engine_manager] = lambda: engine
    try:
        transport = httpx.ASGITransport(app=app)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
                headers = await _register(client)
                source = await client.post(
                    "/api/v1/sources",
                    headers=headers,
                    json={"name": "检索策略测试源"},
                )
                assert source.status_code == 201, source.text

                response = await client.post(
                    "/api/v1/search",
                    headers=headers,
                    json={
                        "query": "策略测试",
                        "source_ids": [source.json()["id"]],
                        "strategy": "multi",
                        "top_k": 7,
                    },
                )
                assert response.status_code == 200, response.text
                assert engine.strategy == "multi"
                # 对外仍返回 7 条；内部有界扩大候选池，之后统一重排与过滤。
                assert engine.top_k == 21
                assert engine.event_top_k == 7
                assert engine.started == {"chunks", "events"}
                assert response.json()["stats"]["strategy"] == "multi"
                result = response.json()
                assert result["stats"]["requested_top_k"] == 7
                assert result["stats"]["candidate_top_k"] == 21
                assert result["stats"]["event_candidates"] == 1
                assert result["stats"]["event_hits"] == 1
                assert result["stats"]["event_recall"] == "vector+chunk"
                assert "[1]" in result["summary"]
                assert result["events"][0]["title"] == "外卖骑手收入变化"
                assert result["events"][0]["chunk_id"] == "event-chunk-not-in-section-results"
                assert result["events"][0]["summary"].startswith("报告分析")
                assert result["events"][0]["source_id"] == source.json()["id"]
                assert result["entities"][0]["name"] == "外卖骑手"
                assert result["relations"][0]["kind"] == "mentions"

                deprecated = await client.post(
                    "/api/v1/search",
                    headers=headers,
                    json={"query": "策略测试", "strategy": "atomic"},
                )
                assert deprecated.status_code == 422

                invalid = await client.post(
                    "/api/v1/search",
                    headers=headers,
                    json={"query": "策略测试", "strategy": "unknown"},
                )
                assert invalid.status_code == 422
    finally:
        app.dependency_overrides.pop(get_engine_manager, None)


@pytest.mark.asyncio
async def test_search_many_caps_candidates_and_concurrency(monkeypatch):
    from sag_api.core.config import settings
    from sag_api.sag.dto import SearchOutcome
    from sag_api.sag.engine_manager import EngineManager

    monkeypatch.setattr(settings, "search_source_candidate_limit", 2)
    monkeypatch.setattr(settings, "search_source_concurrency", 1)
    manager = EngineManager(settings)
    active = 0
    peak = 0
    calls: list[str] = []

    async def fake_search(source_config_id, query, **_kwargs):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        calls.append(source_config_id)
        await asyncio.sleep(0)
        active -= 1
        return SearchOutcome(query=query, sections=[])

    monkeypatch.setattr(manager, "search", fake_search)
    outcome = await manager.search_many(
        [(f"source-{index}", None) for index in range(5)],
        "有界检索",
        strategy="multi",
    )

    assert calls == ["source-0", "source-1"]
    assert peak == 1
    assert outcome.stats == {
        "sources": 2,
        "sources_requested": 5,
        "source_limit_applied": True,
        "candidates": 0,
        "requested_strategy": "multi",
        "effective_strategy": "multi",
        "fallback_used": False,
    }


@pytest.mark.asyncio
async def test_vector_search_many_uses_one_cross_source_embedding(monkeypatch):
    from zleap.sag.core.storage import client as storage_client
    from zleap.sag.core.storage.repositories.source_chunk_repository import (
        SourceChunkRepository,
    )
    from zleap.sag.modules.load.processor import DocumentProcessor

    from sag_api.core.config import settings
    from sag_api.sag.engine_manager import EngineManager

    manager = EngineManager(settings)
    embedding_queries: list[str] = []
    repository_calls: list[tuple[int, list[str]]] = []

    async def runtime_ready(_sources):
        return None

    async def generate_embedding(_processor, query):
        embedding_queries.append(query)
        return [0.1, 0.2]

    async def search_chunks(
        _repository,
        *,
        query_vector,
        k,
        source_config_ids,
        **_kwargs,
    ):
        assert query_vector == [0.1, 0.2]
        repository_calls.append((k, source_config_ids))
        return [
            {
                "chunk_id": "chunk-2",
                "source_id": "document-2",
                "source_config_id": "source-2",
                "heading": "跨源命中",
                "content": "只生成一次查询向量。",
                "rank": 3,
                "_score": 0.88,
            }
        ]

    monkeypatch.setattr(manager, "_ensure_read_runtime", runtime_ready)
    monkeypatch.setattr(DocumentProcessor, "generate_embedding", generate_embedding)
    monkeypatch.setattr(SourceChunkRepository, "search_similar_by_content", search_chunks)
    monkeypatch.setattr(storage_client, "get_es_client", lambda: object())

    outcome = await manager.search_many(
        [("source-1", None), ("source-2", None)],
        "跨源查询",
        strategy="vector",
        top_k=9,
    )

    assert embedding_queries == ["跨源查询"]
    assert repository_calls == [(9, ["source-1", "source-2"])]
    assert outcome.sections[0].chunk_id == "chunk-2"
    assert outcome.stats["chunk_recall"] == "batch-vector"


@pytest.mark.asyncio
async def test_batch_vector_timeout_does_not_pay_legacy_timeout_again(monkeypatch):
    from sag_api.core.config import settings
    from sag_api.sag.engine_manager import EngineManager

    manager = EngineManager(settings)

    async def timed_out(*_args, **_kwargs):
        raise TimeoutError

    async def legacy_search(*_args, **_kwargs):  # pragma: no cover - regression guard
        raise AssertionError("timed-out batch recall must not enter legacy fan-out")

    monkeypatch.setattr(manager, "_search_chunk_vectors", timed_out)
    monkeypatch.setattr(manager, "search", legacy_search)

    outcome = await manager.search_many(
        [("source-1", None)],
        "超时仍返回",
        strategy="vector",
        top_k=8,
    )

    assert outcome.sections == []
    assert outcome.stats["chunk_recall"] == "batch-vector-timeout"


@pytest.mark.asyncio
async def test_single_source_timeout_includes_lock_queue(monkeypatch):
    from sag_api.core.config import settings
    from sag_api.sag.engine_manager import EngineManager

    monkeypatch.setattr(settings, "search_source_timeout", 1.0)
    manager = EngineManager(settings)

    @asynccontextmanager
    async def blocked_use(*_args, **_kwargs):
        await asyncio.Event().wait()
        yield  # pragma: no cover - timeout must happen before acquisition

    monkeypatch.setattr(manager, "use", blocked_use)
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        await manager._search_raw(
            "source-queued",
            "排队超时",
            source=None,
            strategy="vector",
            top_k=5,
        )

    assert time.monotonic() - started < 1.5


@pytest.mark.asyncio
async def test_search_source_candidates_use_database_limit_and_explicit_order(monkeypatch):
    from sag_api.core.config import settings
    from sag_api.core.db import SessionLocal
    from sag_api.core.errors import ValidationError
    from sag_api.db.models import Source
    from sag_api.main import app
    from sag_api.services.source_service import search_source_candidates

    monkeypatch.setattr(settings, "search_source_candidate_limit", 2)
    ids = [uuid.uuid4().hex for _ in range(3)]
    async with app.router.lifespan_context(app):
        async with SessionLocal() as session:
            session.add_all(
                [
                    Source(
                        id=source_id,
                        name=f"候选源 {index}",
                        sag_source_config_id=f"candidate-{source_id}",
                        chunk_count=10_000 + index,
                        event_count=index,
                    )
                    for index, source_id in enumerate(ids)
                ]
            )
            await session.commit()

            implicit = await search_source_candidates(session)
            explicit = await search_source_candidates(session, [ids[0], ids[2]])
            with pytest.raises(ValidationError) as captured:
                await search_source_candidates(session, ids)

            assert [source.id for source in implicit] == [ids[2], ids[1]]
            assert [source.id for source in explicit] == [ids[0], ids[2]]
            assert captured.value.code == "too_many_search_sources"

            await session.execute(delete(Source).where(Source.id.in_(ids)))
            await session.commit()


@pytest.mark.asyncio
async def test_multi_es_fast_translates_to_zleap_multi_es(monkeypatch):
    """门面 multi_es_fast → zleap multi_es;stats.requested/effective 都保留门面名。

    这是 vector vs multi_es_fast 评测能真的分出差异的前提:如果翻译层错把 multi_es_fast
    折成 vector,eval-compare 会给出两列相同结果。
    """
    from sag_api.core.config import settings
    from sag_api.sag.engine_manager import EngineManager

    monkeypatch.setattr(settings, "sag_vector_provider", "lancedb")
    manager = EngineManager(settings)
    captured_strategies: list[str] = []

    @asynccontextmanager
    async def fake_use(*_args, **_kwargs):
        class _Result:
            def __init__(self, query: str):
                self.query = query
                # zleap engine yields dict-shape sections; from_section reads .get.
                self.sections = [
                    {
                        "chunk_id": "c1",
                        "heading": "h",
                        "content": "body",
                        "score": 0.5,
                        "rank": 1,
                        "source_config_id": "cfg-1",
                    }
                ]
                self.stats = {"chunk_recall": "multi_es"}

        class _Engine:
            async def search(self, query, *, strategy, top_k):
                captured_strategies.append(strategy)
                return _Result(query)

        yield _Engine()

    monkeypatch.setattr(manager, "use", fake_use)

    outcome = await manager.search(
        "cfg-1",
        "外卖骑手收入",
        strategy="multi_es_fast",
        top_k=6,
    )

    assert captured_strategies == ["multi_es"]
    assert outcome.stats["requested_strategy"] == "multi_es_fast"
    assert outcome.stats["effective_strategy"] == "multi_es_fast"
    assert outcome.stats["fallback_used"] is False


@pytest.mark.asyncio
async def test_multi_es_fast_gated_by_lexical_capability(monkeypatch):
    """provider 不支持 lexical_search 时,门面策略必须回退到 settings.search_strategy(通常是 vector)。

    Web 端灰置只是提示,后端必须自己拦住,否则会打到不支持 BM25 的向量库。
    """
    from sag_api.core.config import settings
    from sag_api.sag.engine_manager import EngineManager

    monkeypatch.setattr(settings, "sag_vector_provider", "pgvector")
    monkeypatch.setattr(settings, "search_strategy", "vector")
    manager = EngineManager(settings)

    effective = manager._effective_search_strategy("multi_es_fast")
    assert effective == "vector"


def test_strategies_capability_report_marks_multi_es_disabled_on_pgvector(monkeypatch):
    """capabilities API 直接透传的形状。UI 灰置 + tooltip 靠这里返回的 disabled map。"""
    from sag_api.core.config import settings
    from sag_api.sag.engine_manager import EngineManager

    monkeypatch.setattr(settings, "sag_vector_provider", "pgvector")
    report = EngineManager.strategies_capability_report(settings)

    assert set(report["enabled"]) == {"vector", "multi"}
    assert "multi_es_fast" in report["disabled"]
    disabled_entry = report["disabled"]["multi_es_fast"]
    assert disabled_entry["reason"] == "vector_provider_lacks_lexical"
    assert "pgvector" in disabled_entry["message"]

    monkeypatch.setattr(settings, "sag_vector_provider", "lancedb")
    ok_report = EngineManager.strategies_capability_report(settings)
    assert set(ok_report["enabled"]) == {"vector", "multi", "multi_es_fast"}
    assert ok_report["disabled"] == {}


@pytest.mark.asyncio
async def test_capabilities_endpoint_exposes_disabled_strategies(monkeypatch):
    """Web 端读的 /system/capabilities 必须把 disabled map 透传给前端灰置逻辑。"""
    from sag_api.core.config import settings
    from sag_api.main import app

    monkeypatch.setattr(settings, "sag_vector_provider", "pgvector")

    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            response = await client.get("/api/v1/system/capabilities")

    assert response.status_code == 200, response.text
    payload = response.json()
    assert "multi_es_fast" not in payload["search_strategies"]
    assert "multi_es_fast" in payload["search_strategies_disabled"]
    assert (
        payload["search_strategies_disabled"]["multi_es_fast"]["reason"]
        == "vector_provider_lacks_lexical"
    )


@pytest.mark.asyncio
async def test_eval_compare_returns_two_strategies_and_skips_judge(monkeypatch):
    """/search/eval-compare 的端到端形状:两列结果 + judge 因 LLM 未配置被跳过。

    模拟 curl POST。真跑要求本地 LLM 已配置,这里只覆盖形状 + 关键字段(strategy 命名、
    stats 保留 requested_strategy、judge_reason 说明未运行原因)。
    """
    from sag_api.core.config import settings
    from sag_api.core.deps import get_engine_manager, get_llm
    from sag_api.main import app
    from sag_api.sag.dto import RetrievedSection, SearchOutcome

    monkeypatch.setattr(settings, "sag_vector_provider", "lancedb")

    class StubEngine:
        async def provision(self, *_args, **_kwargs):
            return None

        async def search_many(self, targets, query, *, strategy=None, top_k=None):
            source_config_id = targets[0][0] if targets else "cfg-0"
            # Vector vs multi_es_fast: 用不同 heading 让两列可视化上真的不一样,
            # 从而证明翻译层能触达 zleap 引擎、而不是折成同一个 pipeline。
            heading = (
                "vector-hit" if strategy == "vector" else "multi_es-hit"
            )
            return SearchOutcome(
                query=query,
                sections=[
                    RetrievedSection(
                        chunk_id=f"chunk-{strategy}",
                        heading=heading,
                        content="片段正文",
                        score=0.8,
                        source_config_id=source_config_id,
                    )
                ],
                stats={
                    "requested_strategy": strategy,
                    "effective_strategy": strategy,
                    "fallback_used": False,
                    "candidates": 1,
                    "relevant": 1,
                },
            )

    class NoLLM:
        configured = False

    app.dependency_overrides[get_engine_manager] = lambda: StubEngine()
    app.dependency_overrides[get_llm] = lambda: NoLLM()

    try:
        transport = httpx.ASGITransport(app=app)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
                headers = await _register_eval(client)
                source = await client.post(
                    "/api/v1/sources",
                    headers=headers,
                    json={"name": "eval-compare 测试源"},
                )
                assert source.status_code == 201, source.text

                response = await client.post(
                    "/api/v1/search/eval-compare",
                    headers=headers,
                    json={
                        "query": "外卖骑手收入",
                        "strategies": ["vector", "multi_es_fast"],
                        "source_ids": [source.json()["id"]],
                        "top_k": 5,
                        "judge": True,
                    },
                )
    finally:
        app.dependency_overrides.pop(get_engine_manager, None)
        app.dependency_overrides.pop(get_llm, None)

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["query"] == "外卖骑手收入"
    assert [row["strategy"] for row in payload["results"]] == ["vector", "multi_es_fast"]
    # 两列各自透传了本策略名,方便 UI 打标签。
    assert payload["results"][0]["stats"]["requested_strategy"] == "vector"
    assert payload["results"][1]["stats"]["requested_strategy"] == "multi_es_fast"
    # 两列结果确实不同(证明翻译层没把 multi_es_fast 折成 vector)。
    assert (
        payload["results"][0]["sections"][0]["heading"]
        != payload["results"][1]["sections"][0]["heading"]
    )
    # LLM 未配置时 judge 被显式禁用,且给出人可读的原因。
    assert payload["judge_enabled"] is False
    assert payload["judges"] == []
    assert payload["judge_reason"] is not None


async def _register_eval(client: httpx.AsyncClient) -> dict[str, str]:
    """独立的注册辅助,避免和主 register 复用同一个邮箱触发唯一约束。"""
    response = await client.post(
        "/api/v1/auth/register",
        json={"email": "eval-compare@t.com", "password": "password123"},
    )
    assert response.status_code == 201, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


@pytest.mark.asyncio
async def test_vector_search_excludes_hidden_document_sources_before_top_k(
    monkeypatch,
):
    from zleap.sag.core.storage import client as storage_client
    from zleap.sag.modules.load.processor import DocumentProcessor

    from sag_api.core.config import settings
    from sag_api.sag.engine_manager import EngineManager

    class VectorClient:
        def __init__(self):
            self.filter_query = None

        async def vector_search(
            self,
            *,
            index,
            field,
            vector,
            size,
            filter_query,
            **_kwargs,
        ):
            assert index == "source_chunks"
            assert field == "content_vector"
            assert vector == [0.1, 0.2]
            assert size == 8
            self.filter_query = filter_query
            return [
                {
                    "chunk_id": "visible-chunk",
                    "source_id": "visible-document",
                    "source_config_id": "source-1",
                    "heading": "健康文档",
                    "content": "删除失败的文档不能占满候选。",
                    "rank": 0,
                    "_score": 0.9,
                }
            ]

    manager = EngineManager(settings)
    vector_client = VectorClient()

    async def runtime_ready(_sources):
        return None

    async def generate_embedding(_processor, _query):
        return [0.1, 0.2]

    monkeypatch.setattr(manager, "_ensure_read_runtime", runtime_ready)
    monkeypatch.setattr(DocumentProcessor, "generate_embedding", generate_embedding)
    monkeypatch.setattr(storage_client, "get_es_client", lambda: vector_client)

    outcome = await manager.search_many(
        [("source-1", None), ("source-2", None)],
        "目标主题",
        strategy="vector",
        top_k=8,
        exclude_source_ids_by_config={
            "source-1": ("hidden-a", "hidden-b"),
            "source-2": ("hidden-c",),
        },
    )

    assert [section.chunk_id for section in outcome.sections] == ["visible-chunk"]
    assert vector_client.filter_query == {
        "bool": {
            "filter": [{"terms": {"source_config_id": ["source-1", "source-2"]}}],
            "must_not": [
                {
                    "bool": {
                        "filter": [
                            {"term": {"source_config_id": "source-1"}},
                            {"terms": {"source_id": ["hidden-a", "hidden-b"]}},
                        ]
                    }
                },
                {
                    "bool": {
                        "filter": [
                            {"term": {"source_config_id": "source-2"}},
                            {"terms": {"source_id": ["hidden-c"]}},
                        ]
                    }
                },
            ],
        }
    }


@pytest.mark.asyncio
async def test_grep_excludes_hidden_document_source_before_limit():
    from zleap.sag.db import get_session_factory
    from zleap.sag.db.models import SourceChunk

    from sag_api.core.config import settings
    from sag_api.sag.engine_manager import EngineManager

    source_config_id = f"grep-prefilter-{uuid.uuid4().hex}"
    manager = EngineManager(settings)
    try:
        await manager.provision(source_config_id)
        async with get_session_factory()() as session:
            session.add_all(
                [
                    SourceChunk(
                        id=uuid.uuid4().hex,
                        source_config_id=source_config_id,
                        source_type="ARTICLE",
                        source_id="hidden-document",
                        heading="隐藏文档",
                        content="唯一关键词",
                        rank=0,
                    ),
                    SourceChunk(
                        id=uuid.uuid4().hex,
                        source_config_id=source_config_id,
                        source_type="ARTICLE",
                        source_id="visible-document",
                        heading="健康文档",
                        content="唯一关键词",
                        rank=1,
                    ),
                ]
            )
            await session.commit()

        rows = await manager.grep_chunks(
            source_config_id,
            "唯一关键词",
            limit=1,
            exclude_source_ids=("hidden-document",),
        )
    finally:
        await manager.aclose_all()

    assert [row["source_id"] for row in rows] == ["visible-document"]


@pytest.mark.asyncio
async def test_multi_search_uses_prefiltered_batch_recall_when_sources_are_hidden(
    monkeypatch,
):
    from sag_api.core.config import settings
    from sag_api.sag import RetrievedSection, SearchOutcome
    from sag_api.sag.engine_manager import EngineManager

    manager = EngineManager(settings)
    batch_calls = 0
    legacy_calls = 0

    async def batch_recall(
        _targets,
        query,
        *,
        top_k,
        requested_sources,
        exclude_source_ids_by_config,
    ):
        nonlocal batch_calls
        batch_calls += 1
        assert exclude_source_ids_by_config == {"source-1": ("hidden-document",)}
        return SearchOutcome(
            query=query,
            sections=[
                RetrievedSection(
                    chunk_id="visible-chunk",
                    heading="健康文档",
                    content="精确模式在删除屏障期间安全降级。",
                    score=0.9,
                    source_id="visible-document",
                    source_config_id="source-1",
                )
            ],
            stats={"chunk_recall": "batch-vector"},
        )

    async def legacy_search(*_args, **_kwargs):
        nonlocal legacy_calls
        legacy_calls += 1
        return SearchOutcome(query="目标主题", sections=[])

    monkeypatch.setattr(manager, "_search_chunk_vectors", batch_recall)
    monkeypatch.setattr(manager, "search", legacy_search)

    outcome = await manager.search_many(
        [("source-1", None)],
        "目标主题",
        strategy="multi_es_fast",
        top_k=8,
        exclude_source_ids_by_config={"source-1": ("hidden-document",)},
    )

    assert [section.chunk_id for section in outcome.sections] == ["visible-chunk"]
    assert batch_calls == 1
    assert legacy_calls == 0
    assert outcome.stats["requested_strategy"] == "multi_es_fast"
    assert outcome.stats["effective_strategy"] == "vector"


@pytest.mark.asyncio
async def test_visibility_prefilter_failure_never_falls_back_to_unfiltered_legacy(
    monkeypatch,
):
    from sag_api.core.config import settings
    from sag_api.sag import RetrievedSection, SearchOutcome
    from sag_api.sag.engine_manager import EngineManager

    manager = EngineManager(settings)
    legacy_calls = 0

    async def broken_prefilter(*_args, **_kwargs):
        raise RuntimeError("vector backend unavailable")

    async def unfiltered_legacy(*_args, **_kwargs):
        nonlocal legacy_calls
        legacy_calls += 1
        return SearchOutcome(
            query="目标主题",
            sections=[
                RetrievedSection(
                    chunk_id="hidden-chunk",
                    heading="隐藏文档",
                    content="不能回退到未过滤候选。",
                    score=0.99,
                    source_id="hidden-document",
                    source_config_id="source-1",
                )
            ],
        )

    monkeypatch.setattr(manager, "_search_chunk_vectors", broken_prefilter)
    monkeypatch.setattr(manager, "search", unfiltered_legacy)

    outcome = await manager.search_many(
        [("source-1", None)],
        "目标主题",
        strategy="multi_es_fast",
        top_k=8,
        exclude_source_ids_by_config={"source-1": ("hidden-document",)},
    )

    assert outcome.sections == []
    assert legacy_calls == 0
    assert outcome.stats["chunk_recall"] == "batch-vector-prefilter-failed"
