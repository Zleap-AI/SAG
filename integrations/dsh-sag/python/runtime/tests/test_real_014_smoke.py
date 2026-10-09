import hashlib
import importlib.metadata

import pytest
from zleap.sag import DataEngine, EngineConfig
from zleap.sag.config import EmbeddingConfig, LLMConfig
from zleap.sag.operations import OperationContext, ProcessSourceRequest
from zleap.sag.pipeline import ArticleSource, ExtractionOptions, SourceDescriptor

from dsh_sag_runtime.engines import EnginePool
from dsh_sag_runtime.evidence import EvidenceRefCodec
from dsh_sag_runtime.protocol import ReadParams, SearchParams
from dsh_sag_runtime.read import ReadAdapter
from dsh_sag_runtime.search import SearchAdapter

CONTENT = "# 上传说明\n\nDW-2412P30 单文件上传上限为 100 MiB。"


def vector(text: str) -> list[float]:
    digest = hashlib.sha256(text.encode()).digest()
    return [0.1 + digest[index] / 283.0 for index in range(32)]


class FakeEmbedding:
    async def generate(self, text: str) -> list[float]:
        return vector(text)

    async def batch_generate(self, texts: list[str]) -> list[list[float]]:
        return [vector(text) for text in texts]


@pytest.mark.asyncio
async def test_real_014_storage_search_read_and_shutdown(monkeypatch, tmp_path) -> None:
    assert importlib.metadata.version("zleap-sag") == "0.14.0"

    async def get_embedding(_self):
        return FakeEmbedding()

    async def extract(_self, _messages, response_schema=None, **_kwargs):
        del response_schema
        return {
            "type": "response",
            "data": {"items": [{
                "title": "单文件上传限制",
                "content": "DW-2412P30 的上传上限是 100 MiB。",
                "entities": [{"type": "product", "name": "DW-2412P30", "description": "产品型号"}],
                "is_valid": True,
            }]},
        }

    monkeypatch.setattr("zleap.sag.core.adapters.defaults.OpenAIEmbeddingAdapter._get", get_embedding)
    from zleap.sag.core.ai.base import BaseLLMClient, LLMRetryClient
    monkeypatch.setattr(BaseLLMClient, "chat_with_schema", extract)
    monkeypatch.setattr(LLMRetryClient, "chat_with_schema", extract)

    namespace = "product-docs"
    source = "manual"
    config = EngineConfig(
        storage_mode="normal",
        data_dir=str(tmp_path / "storage"),
        llm=LLMConfig(api_key="stub", model="stub"),
        embedding=EmbeddingConfig(model="stub", schema_dimensions=32),
    )
    request = ProcessSourceRequest(
        context=OperationContext(
            operation_id="seed-operation", idempotency_key="seed-v1",
            request_digest=hashlib.sha256(CONTENT.encode()).hexdigest(),
            fence_scope=namespace, fence_token=1, owner_id="smoke", lease_seconds=30,
        ),
        source=ArticleSource(
            content=CONTENT,
            descriptor=SourceDescriptor(
                data_source_id=namespace, source_id=source, source_type="article", title="上传说明",
            ),
        ),
        extraction_options=ExtractionOptions(contract="minimal"),
    )
    async with DataEngine(config, data_source_id=namespace) as writer:
        seeded = await writer.process_source(request)
        assert seeded.status == "succeeded"

    pool = EnginePool(config, {namespace}, max_read_engines=1)
    await pool.start()
    codec = EvidenceRefCodec({namespace})
    search = SearchAdapter(pool, codec, {namespace}, max_results=5, max_excerpt_chars=200)
    read = ReadAdapter(pool, codec, max_read_chars=200, max_events=5)
    try:
        found = await search.search(SearchParams(query="DW-2412P30 上传限制", namespaces=(namespace,), mode="fast", limit=5))
        assert found["evidences"]
        page = await read.read(ReadParams(
            evidenceRef=found["evidences"][0]["evidenceRef"], offset=0, maxChars=200, includeEvents=True,
        ))
        assert "DW-2412P30" in page["content"]
    finally:
        await pool.aclose()
