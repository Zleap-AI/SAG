from dataclasses import dataclass

import pytest
from zleap.sag.pipeline import SearchHit, SearchResult

from dsh_sag_runtime.evidence import EvidenceRefCodec
from dsh_sag_runtime.protocol import SearchParams
from dsh_sag_runtime.search import SearchAdapter


class FakeEngine:
    def __init__(self, result: SearchResult) -> None:
        self.result = result
        self.requests: list[object] = []

    async def search(self, request: object) -> SearchResult:
        self.requests.append(request)
        return self.result


@dataclass
class FakePool:
    engine: FakeEngine

    async def search_engine(self) -> FakeEngine:
        return self.engine


def result(*hits: SearchHit, events: tuple[SearchHit, ...] = ()) -> SearchResult:
    return SearchResult(query="q", original_query="q", chunks=hits, events=events)


@pytest.mark.asyncio
async def test_search_maps_fast_mode_to_contract_20_and_stable_evidence() -> None:
    engine = FakeEngine(result(SearchHit(
        id="hit-1", data_source_id="product-docs", source_id="manual", chunk_id="chunk-1",
        title="上传说明", content="中文内容", score=0.91,
    )))
    adapter = SearchAdapter(FakePool(engine), EvidenceRefCodec({"product-docs"}), {"product-docs"}, max_results=20, max_excerpt_chars=200)

    value = await adapter.search(SearchParams(query="DW-2412P30 上传限制", namespaces=("product-docs",), mode="fast", limit=8))

    request = engine.requests[0]
    assert request.query == "DW-2412P30 上传限制"
    assert request.scope.data_source_ids == ("product-docs",)
    assert request.options.strategy == "vector"
    assert request.options.top_k == 8
    assert request.options.return_type == "chunk"
    assert request.options.output.include_chunk is False
    assert value["evidences"][0] == {
        "evidenceRef": EvidenceRefCodec({"product-docs"}).encode_from_parts("product-docs", "manual", "chunk-1"),
        "namespaceId": "product-docs",
        "sourceId": "manual",
        "title": "上传说明",
        "excerpt": "中文内容",
        "score": 0.91,
    }


@pytest.mark.asyncio
async def test_search_precise_mode_caps_deduplicates_and_truncates() -> None:
    duplicate = SearchHit(id="2", data_source_id="a", source_id="s", chunk_id="c", content="second")
    engine = FakeEngine(result(
        SearchHit(id="1", data_source_id="a", source_id="s", chunk_id="c", content="😀中文abcdef"),
        duplicate,
        SearchHit(id="3", data_source_id="a", source_id="s", chunk_id="d", content="other"),
    ))
    adapter = SearchAdapter(FakePool(engine), EvidenceRefCodec({"a"}), {"a"}, max_results=1, max_excerpt_chars=4)

    value = await adapter.search(SearchParams(query="q", namespaces=("a",), mode="precise", limit=10))

    assert engine.requests[0].options.strategy == "full_expand"
    assert engine.requests[0].options.top_k == 1
    assert [item["excerpt"] for item in value["evidences"]] == ["😀中文a"]


@pytest.mark.asyncio
async def test_search_rejects_unconfigured_namespace_and_unreadable_hits() -> None:
    adapter = SearchAdapter(FakePool(FakeEngine(result())), EvidenceRefCodec({"a"}), {"a"}, max_results=8, max_excerpt_chars=200)
    with pytest.raises(ValueError, match="not configured"):
        await adapter.search(SearchParams(query="q", namespaces=("b",), mode="fast", limit=1))

    engine = FakeEngine(result(SearchHit(id="1", data_source_id="a", source_id=None, chunk_id="c", content="x")))
    adapter = SearchAdapter(FakePool(engine), EvidenceRefCodec({"a"}), {"a"}, max_results=8, max_excerpt_chars=200)
    with pytest.raises(ValueError, match="readable identifiers"):
        await adapter.search(SearchParams(query="q", namespaces=("a",), mode="fast", limit=1))


@pytest.mark.asyncio
async def test_search_uses_event_hits_only_when_chunks_are_empty() -> None:
    event = SearchHit(id="e", data_source_id="a", source_id="s", chunk_id="c", title="event", content="summary")
    engine = FakeEngine(result(events=(event,)))
    adapter = SearchAdapter(FakePool(engine), EvidenceRefCodec({"a"}), {"a"}, max_results=8, max_excerpt_chars=200)

    value = await adapter.search(SearchParams(query="q", namespaces=("a",), mode="precise", limit=8))

    assert value["evidences"][0]["title"] == "event"
