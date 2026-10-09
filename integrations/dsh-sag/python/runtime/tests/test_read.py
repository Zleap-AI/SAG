from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from dsh_sag_runtime.evidence import EvidenceLocator, EvidenceRefCodec
from dsh_sag_runtime.protocol import ReadParams
from dsh_sag_runtime.read import ReadAdapter


class FakeEngine:
    def __init__(self, records: tuple[object, ...]) -> None:
        self.records = records
        self.calls: list[tuple[str, tuple[str, ...], int]] = []

    async def read_evidence(self, source_id: str, chunk_ids: tuple[str, ...], *, max_events_per_chunk: int) -> tuple[object, ...]:
        self.calls.append((source_id, chunk_ids, max_events_per_chunk))
        return self.records


class FakePool:
    def __init__(self, engine: FakeEngine) -> None:
        self.engine = engine
        self.namespaces: list[str] = []

    @asynccontextmanager
    async def read_engine(self, namespace: str):
        self.namespaces.append(namespace)
        yield self.engine


def record(content: str, *, chunk_id: str = "chunk", events: tuple[object, ...] = ()) -> object:
    chunk = SimpleNamespace(id=chunk_id, heading="上传说明", content=content, source_id="manual")
    return SimpleNamespace(chunk=chunk, events=events)


@pytest.mark.asyncio
async def test_read_resolves_exact_evidence_and_pages_unicode_by_code_point() -> None:
    event = SimpleNamespace(id="e1", title="限制", summary="最大 100 MiB", category="rule", rank=2, content="secret")
    engine = FakeEngine((record("中文😀abcdef", events=(event,)),))
    pool = FakePool(engine)
    codec = EvidenceRefCodec({"product-docs"})
    reference = codec.encode(EvidenceLocator("product-docs", "manual", "chunk"))
    adapter = ReadAdapter(pool, codec, max_read_chars=4, max_events=10)

    value = await adapter.read(ReadParams(evidenceRef=reference, offset=1, maxChars=50, includeEvents=True))

    assert pool.namespaces == ["product-docs"]
    assert engine.calls == [("manual", ("chunk",), 10)]
    assert value == {
        "title": "上传说明",
        "content": "文😀ab",
        "offset": 1,
        "nextOffset": 5,
        "totalChars": 9,
        "events": [{"id": "e1", "title": "限制", "summary": "最大 100 MiB", "category": "rule", "rank": 2}],
    }


@pytest.mark.asyncio
async def test_read_omits_next_offset_at_eof_and_can_omit_events() -> None:
    codec = EvidenceRefCodec({"a"})
    reference = codec.encode(EvidenceLocator("a", "s", "c"))
    adapter = ReadAdapter(FakePool(FakeEngine((record("abc", chunk_id="c"),))), codec, max_read_chars=10, max_events=3)

    value = await adapter.read(ReadParams(evidenceRef=reference, offset=3, maxChars=10, includeEvents=False))

    assert value == {"title": "上传说明", "content": "", "offset": 3, "totalChars": 3}


@pytest.mark.asyncio
async def test_read_rejects_missing_duplicate_or_out_of_range_evidence() -> None:
    codec = EvidenceRefCodec({"a"})
    reference = codec.encode(EvidenceLocator("a", "s", "c"))
    for records in ((), (record("a", chunk_id="c"), record("b", chunk_id="c"))):
        adapter = ReadAdapter(FakePool(FakeEngine(records)), codec, max_read_chars=10, max_events=3)
        with pytest.raises(ValueError, match="exactly one"):
            await adapter.read(ReadParams(evidenceRef=reference, offset=0, maxChars=10, includeEvents=False))

    adapter = ReadAdapter(FakePool(FakeEngine((record("abc", chunk_id="c"),))), codec, max_read_chars=10, max_events=3)
    with pytest.raises(ValueError, match="beyond"):
        await adapter.read(ReadParams(evidenceRef=reference, offset=4, maxChars=10, includeEvents=False))
