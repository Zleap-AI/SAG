"""Search SSE returns stable evidence first and a validated canonical answer last."""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from types import SimpleNamespace

import httpx
import pytest

from sag_api.core.config import Settings
from sag_api.core.errors import UpstreamError
from sag_api.generation import LLMClient
from sag_api.sag import RetrievedSection, SearchOutcome, SourceGraphInfo
from sag_api.services.retrieval_service import stream_synthesize_search_answer


class SearchEngine:
    async def provision(self, *_args):
        return None

    async def search_many(self, targets, query, *, strategy=None, top_k=None):
        return SearchOutcome(
            query=query,
            sections=[
                RetrievedSection(
                    chunk_id="chunk-1",
                    heading="骑手技能证据",
                    content="骑手技能包括路线规划和异常处理。",
                    score=0.91,
                    source_config_id=targets[0][0],
                )
            ],
            stats={"strategy": strategy, "top_k": top_k},
        )

    async def graph_for_sections(self, *_args, **_kwargs):
        return SourceGraphInfo()


class StreamingLLM:
    configured = True

    def __init__(self, deltas: list[str]):
        self.deltas = deltas
        self.stream_calls = 0

    async def stream_complete(self, _messages):
        self.stream_calls += 1
        for delta in self.deltas:
            await asyncio.sleep(0)
            yield delta

    async def complete(self, _messages):  # pragma: no cover - protocol guard
        raise AssertionError("stream endpoint must not call complete()")


class FailingStreamingLLM(StreamingLLM):
    async def stream_complete(self, _messages):
        yield "未完成的内容"
        raise UpstreamError("模型连接中断")


def _events(body: str) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    for frame in re.split(r"\r?\n\r?\n", body):
        event = ""
        data: list[str] = []
        for line in frame.splitlines():
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                data.append(line[5:].lstrip())
        if event and data:
            events.append((event, json.loads("\n".join(data))))
    return events


async def _auth_and_source(client: httpx.AsyncClient) -> tuple[dict[str, str], str]:
    registered = await client.post(
        "/api/v1/auth/register",
        json={
            "email": f"search-stream-{uuid.uuid4().hex}@t.com",
            "password": "password123",
        },
    )
    assert registered.status_code == 201, registered.text
    headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}
    source = await client.post(
        "/api/v1/sources",
        headers=headers,
        json={"name": "流式搜索测试源"},
    )
    assert source.status_code == 201, source.text
    return headers, source.json()["id"]


async def _search(
    llm,
    *,
    request_overrides: dict | None = None,
) -> list[tuple[str, dict]]:
    from sag_api.core.deps import get_engine_manager
    from sag_api.main import app

    engine = SearchEngine()
    app.dependency_overrides[get_engine_manager] = lambda: engine
    try:
        transport = httpx.ASGITransport(app=app)
        async with app.router.lifespan_context(app):
            app.state.llm = llm
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
                headers, source_id = await _auth_and_source(client)
                response = await client.post(
                    "/api/v1/search/stream",
                    headers=headers,
                    json={
                        "query": "骑手技能",
                        "source_ids": [source_id],
                        **(request_overrides or {}),
                    },
                )
                assert response.status_code == 200, response.text
                assert response.headers["content-type"].startswith("text/event-stream")
                return _events(response.text)
    finally:
        app.dependency_overrides.pop(get_engine_manager, None)


@pytest.mark.asyncio
async def test_search_stream_emits_true_deltas_then_canonical_response():
    llm = StreamingLLM(["骑手", "需要规划能力", " [1]"])

    events = await _search(llm)

    assert [name for name, _payload in events] == [
        "result",
        "summary.delta",
        "summary.delta",
        "summary.delta",
        "completed",
    ]
    initial = events[0][1]
    assert initial["summary"] == ""
    assert initial["sections"][0]["chunk_id"] == "chunk-1"
    assert [payload["delta"] for name, payload in events if name == "summary.delta"] == [
        "骑手",
        "需要规划能力",
        " [1]",
    ]
    completed = events[-1][1]
    assert completed["summary"] == "骑手需要规划能力 [1]"
    assert completed["sections"] == initial["sections"]
    assert llm.stream_calls == 1


@pytest.mark.asyncio
async def test_search_stream_replaces_invalid_citations_with_grounded_fallback():
    events = await _search(StreamingLLM(["不存在的引用 [9]"]))

    assert [name for name, _payload in events] == [
        "result",
        "summary.delta",
        "completed",
    ]
    assert events[1][1]["delta"] == "不存在的引用 [9]"
    canonical = events[-1][1]["summary"]
    assert "骑手技能包括路线规划和异常处理" in canonical
    assert "[1]" in canonical
    assert "[9]" not in canonical


@pytest.mark.asyncio
async def test_search_stream_provider_failure_completes_with_grounded_fallback():
    events = await _search(FailingStreamingLLM([]))

    assert [name for name, _payload in events] == [
        "result",
        "summary.delta",
        "completed",
    ]
    assert events[1][1]["delta"] == "未完成的内容"
    assert "[1]" in events[-1][1]["summary"]


@pytest.mark.asyncio
async def test_search_stream_emits_terminal_error_when_completion_cannot_be_saved(monkeypatch):
    from sag_api.services import universe_service

    async def fail_save(*_args, **_kwargs):
        raise UpstreamError("探索保存失败")

    monkeypatch.setattr(universe_service, "save_exploration", fail_save)
    events = await _search(
        StreamingLLM(["有效答案 [1]"]),
        request_overrides={"save_exploration": True},
    )

    assert [name for name, _payload in events] == [
        "result",
        "summary.delta",
        "error",
    ]
    assert events[-1][1] == {
        "code": "upstream_error",
        "message": "探索保存失败",
    }


@pytest.mark.asyncio
async def test_search_answer_stream_propagates_cancellation_and_closes_provider():
    entered = asyncio.Event()
    closed = asyncio.Event()

    class BlockingLLM:
        configured = True

        async def stream_complete(self, _messages):
            try:
                yield "部分"
                entered.set()
                await asyncio.Event().wait()
            finally:
                closed.set()

    sections = [
        RetrievedSection(
            chunk_id="chunk-1",
            heading="骑手技能",
            content="骑手需要路线规划能力。",
            score=0.9,
        )
    ]

    async def consume() -> None:
        async for _update in stream_synthesize_search_answer(
            "骑手技能",
            sections,
            llm=BlockingLLM(),
        ):
            pass

    task = asyncio.create_task(consume())
    await asyncio.wait_for(entered.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.wait_for(closed.wait(), timeout=1)


@pytest.mark.asyncio
async def test_llm_plain_text_stream_closes_upstream_on_cancellation(monkeypatch):
    entered = asyncio.Event()

    class ProviderStream:
        closed = False

        async def __aiter__(self):
            yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="部分"))])
            entered.set()
            await asyncio.Event().wait()

        async def close(self):
            self.closed = True

    provider_stream = ProviderStream()

    async def fake_completion(**_kwargs):
        return provider_stream

    monkeypatch.setattr("sag_api.generation.llm._litellm_completion", fake_completion)
    llm = LLMClient(
        Settings(
            _env_file=None,
            llm_api_key="test-key",
            llm_model="test-model",
            llm_temperature=0,
            llm_max_tokens=128,
            llm_extra_body=None,
        )
    )

    async def consume() -> None:
        async for _delta in llm.stream_complete([{"role": "user", "content": "test"}]):
            pass

    task = asyncio.create_task(consume())
    await asyncio.wait_for(entered.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert provider_stream.closed is True
