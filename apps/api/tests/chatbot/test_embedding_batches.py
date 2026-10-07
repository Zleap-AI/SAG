"""Official embedding request limits apply to durable and query-scoped SDK resources."""

import json

import httpx
import pytest
from openai import AsyncOpenAI
from zleap.sag.config import EmbeddingConfig
from zleap.sag.core.adapters import registry

from sag_api.core.chatbot_config import Environment


@pytest.fixture
def embedding_http(monkeypatch):
    requests, clients = [], []
    response_options = {"limit": 20, "fail_batch": None}

    def respond(request):
        body = json.loads(request.content)
        requests.append((request, body))
        texts = body["input"]
        if len(texts) > response_options["limit"] or len(requests) == response_options["fail_batch"]:
            return httpx.Response(400, json={"error": {"message": "Invalid embedding batch"}})
        return httpx.Response(
            200,
            json={
                "object": "list",
                "model": body["model"],
                "data": [
                    {"object": "embedding", "index": index, "embedding": [float(text), 0.0, 1.0]}
                    for index, text in reversed(list(enumerate(texts)))
                ],
                "usage": {"prompt_tokens": len(texts), "total_tokens": len(texts)},
            },
        )

    def create(**kwargs):
        client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        clients.append(client)
        return AsyncOpenAI(**kwargs, http_client=client)

    monkeypatch.setattr("openai.AsyncOpenAI", create)
    return requests, clients, response_options


def create_adapter(isolate, model, endpoint, *, query=False, keyless=False):
    isolate.stock.embedding_model = model
    if query:
        isolate.environment = Environment(
            {
                "SAG_CHATBOT_EMBEDDING_ENABLED": "true",
                "SAG_CHATBOT_EMBEDDING_API_KEY": "" if keyless else "query-key",
                "SAG_CHATBOT_EMBEDDING_BASE_URL": endpoint,
            }
        )
    return registry.create_embedding(
        config=EmbeddingConfig(
            model=model,
            api_key="durable-key",
            base_url="https://stock.invalid/v1" if query else endpoint,
            schema_dimensions=3,
            request_dimensions=3,
            max_retries=0,
        )
    )


@pytest.mark.parametrize("query", [False, True])
@pytest.mark.parametrize(
    "model,endpoint,count,limit,sizes",
    [
        ("embedding-3", "https://open.bigmodel.cn/api/paas/v4", 65, 64, [64, 1]),
        ("qwen3.7-text-embedding", "https://dashscope.aliyuncs.com/compatible-mode/v1", 50, 20, [20, 20, 10]),
        (
            "qwen3.7-text-embedding",
            "https://workspace-123.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
            21,
            20,
            [20, 1],
        ),
        ("text-embedding-v4", "https://dashscope.aliyuncs.com/compatible-mode/v1", 23, 10, [10, 10, 3]),
    ],
)
async def test_official_batches_preserve_vector_order(
    isolate, embedding_http, model, endpoint, count, limit, sizes, query
):
    requests, clients, options = embedding_http
    options["limit"] = limit
    adapter = create_adapter(isolate, model, endpoint, query=query)
    texts = [str(index) for index in range(count)]
    try:
        async with isolate.scope(query=query):
            vectors = await adapter.batch_generate(texts)
        assert vectors == [[float(index), 0.0, 1.0] for index in range(count)]
        assert [len(body["input"]) for _, body in requests] == sizes
        assert [text for _, body in requests for text in body["input"]] == texts
        assert all(str(request.url) == endpoint + "/embeddings" for request, _ in requests)
        assert all(body["model"] == model and body["dimensions"] == 3 for _, body in requests)
        assert all(
            request.headers["authorization"] == ("Bearer query-key" if query else "Bearer durable-key")
            for request, _ in requests
        )
    finally:
        await adapter.close()
    assert clients and all(client.is_closed for client in clients)


@pytest.mark.parametrize("query", [False, True])
@pytest.mark.parametrize(
    "model,endpoint",
    [
        ("qwen3.7-text-embedding", "https://api.302ai.cn/v1"),
        ("embedding-3", "https://api.302ai.cn/v1"),
        ("custom-embedding", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
    ],
)
async def test_other_embedding_services_keep_their_batch_behavior(isolate, embedding_http, model, endpoint, query):
    requests, _, options = embedding_http
    options["limit"] = 100
    adapter = create_adapter(isolate, model, endpoint, query=query)
    try:
        async with isolate.scope(query=query):
            vectors = await adapter.batch_generate([str(index) for index in range(65)])
        assert len(vectors) == 65
        assert [len(body["input"]) for _, body in requests] == [65]
    finally:
        await adapter.close()


async def test_keyless_query_batches_keep_authorization_absent(isolate, embedding_http):
    requests, clients, _ = embedding_http
    adapter = create_adapter(
        isolate, "qwen3.7-text-embedding", "https://dashscope.aliyuncs.com/compatible-mode/v1", query=True, keyless=True
    )
    try:
        async with isolate.scope(query=True):
            vectors = await adapter.batch_generate([str(index) for index in range(21)])
        assert vectors == [[float(index), 0.0, 1.0] for index in range(21)]
        assert [len(body["input"]) for _, body in requests] == [20, 1]
        assert all("authorization" not in request.headers for request, _ in requests)
        assert clients and all(client.is_closed for client in clients)
    finally:
        await adapter.close()


@pytest.mark.parametrize("query", [False, True])
async def test_failed_sub_batch_does_not_return_partial_vectors_or_request_later_batches(
    isolate, embedding_http, query
):
    from zleap.sag.exceptions import AIError

    from sag_api.core.errors import UpstreamError

    requests, clients, options = embedding_http
    options["fail_batch"] = 2
    adapter = create_adapter(
        isolate, "qwen3.7-text-embedding", "https://dashscope.aliyuncs.com/compatible-mode/v1", query=query
    )
    try:
        with pytest.raises(UpstreamError if query else AIError):
            async with isolate.scope(query=query):
                await adapter.batch_generate([str(index) for index in range(50)])
        assert [len(body["input"]) for _, body in requests] == [20, 20]
    finally:
        await adapter.close()
    assert clients and all(client.is_closed for client in clients)
