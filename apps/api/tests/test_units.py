"""快速单元测试：无需网络 / 引擎。"""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from sag_api.connectors import registry
from sag_api.core.config import Settings, settings
from sag_api.core.litellm_policy import (
    apply_litellm_completion_policy,
    install_litellm_policy,
    uninstall_litellm_policy,
)
from sag_api.core.model_providers import get_model_provider, model_provider_catalog
from sag_api.core.security import hash_password, verify_password
from sag_api.enums import ConnectorKind
from sag_api.generation.prompt import build_agent_messages, build_citations, build_messages
from sag_api.sag import GraphEventInfo, RetrievedSection
from sag_api.sag.config_builder import build_engine_config


def test_password_hash_roundtrip():
    h = hash_password("password123")
    assert verify_password("password123", h)
    assert not verify_password("wrong", h)


def test_connector_registry():
    conn = registry.get(ConnectorKind.FILE_UPLOAD)
    assert conn.meta.kind == ConnectorKind.FILE_UPLOAD
    assert conn.meta.supports_sync is False
    assert any(c.meta.kind == ConnectorKind.FILE_UPLOAD for c in registry.all())


def test_model_provider_registry_is_the_public_source_of_truth():
    catalog = model_provider_catalog()
    assert [provider["id"] for provider in catalog] == ["openai", "anthropic", "gemini"]
    assert all("litellm_prefix" not in provider for provider in catalog)
    assert get_model_provider("openai").route_model("qwen3.6-flash") == "openai/qwen3.6-flash"
    assert get_model_provider("gemini").route_model("gemini/gemini-3.5-flash") == "gemini/gemini-3.5-flash"


def test_build_engine_config_zero_infra():
    cfg = build_engine_config(settings)
    assert cfg.vector_provider == "lancedb"  # 默认零依赖向量后端
    assert cfg.llm.model == settings.routed_llm_model
    assert cfg.llm.max_tokens == settings.llm_max_tokens
    assert cfg.llm.provider == "litellm"
    assert cfg.data_dir == settings.data_dir


@pytest.mark.parametrize(
    ("provider", "model", "expected_model"),
    [
        ("openai", "qwen3.6-flash", "openai/qwen3.6-flash"),
        ("anthropic", "claude-sonnet-5", "anthropic/claude-sonnet-5"),
        ("gemini", "gemini-3.5-flash", "gemini/gemini-3.5-flash"),
    ],
)
def test_extraction_engine_uses_one_litellm_transport(provider, model, expected_model):
    configured = Settings(
        _env_file=None,
        llm_provider=provider,
        llm_base_url=None,
        llm_api_key="provider-key",
        llm_model=model,
    )

    engine = build_engine_config(configured)

    assert engine.llm.provider == "litellm"
    assert engine.llm.model == expected_model


@pytest.mark.parametrize(
    ("extra_body", "expected_reasoning", "expect_extra_body"),
    [
        (None, "none", False),
        ({"enable_thinking": False}, "none", True),
        ({"chat_template_kwargs": {"enable_thinking": False}}, "none", True),
        ({"enable_thinking": True}, None, True),
    ],
)
def test_litellm_policy_maps_qwen_thinking_option(extra_body, expected_reasoning, expect_extra_body):
    configured = Settings(
        _env_file=None,
        llm_provider="openai",
        llm_api_key="provider-key",
        llm_extra_body=extra_body,
    )
    request = apply_litellm_completion_policy(
        configured,
        {"model": configured.routed_llm_model, "messages": []},
    )

    assert request.get("reasoning_effort") == expected_reasoning
    assert ("extra_body" in request) is expect_extra_body
    assert ("reasoning_effort" in request.get("allowed_openai_params", [])) is (expected_reasoning is not None)


def test_litellm_policy_preserves_explicit_reasoning_and_allowed_params():
    configured = Settings(_env_file=None, llm_api_key="provider-key")

    request = apply_litellm_completion_policy(
        configured,
        {
            "model": "openai/qwen3.6-flash",
            "messages": [],
            "reasoning_effort": "low",
            "allowed_openai_params": ["seed"],
        },
    )

    assert request["reasoning_effort"] == "low"
    assert request["allowed_openai_params"] == ["seed", "reasoning_effort"]


@pytest.mark.parametrize("tool_choice", ["none", "required", None])
@pytest.mark.parametrize(
    ("base_url", "model"),
    [
        ("https://api.deepseek.com", "deepseek-v4-flash"),
        ("https://api.deepseek.com", "deepseek-v4-pro"),
        ("https://openai-compatible.example.com/v1", "deepseek/deepseek-v4-flash"),
    ],
)
def test_deepseek_v4_disables_thinking_for_every_agent_tool_turn(tool_choice, base_url, model):
    configured = Settings(
        _env_file=None,
        llm_provider="openai",
        llm_base_url=base_url,
        llm_api_key="provider-key",
        llm_model=model,
    )
    raw = {
        "model": configured.routed_llm_model,
        "messages": [],
        "tools": [{"type": "function", "function": {"name": "search_context"}}],
    }
    if tool_choice is not None:
        raw["tool_choice"] = tool_choice

    request = apply_litellm_completion_policy(configured, raw)

    assert request["extra_body"]["thinking"] == {"type": "disabled"}
    if tool_choice is None:
        assert "tool_choice" not in request
    else:
        assert request["tool_choice"] == tool_choice


def test_deepseek_v4_plain_completion_disables_thinking():
    configured = Settings(
        _env_file=None,
        llm_provider="openai",
        llm_base_url="https://api.deepseek.com",
        llm_api_key="provider-key",
        llm_model="deepseek-v4-pro",
    )

    request = apply_litellm_completion_policy(
        configured,
        {"model": configured.routed_llm_model, "messages": []},
    )

    assert request["extra_body"]["thinking"] == {"type": "disabled"}


def test_deepseek_v4_tool_policy_preserves_other_extra_body_fields():
    configured = Settings(
        _env_file=None,
        llm_provider="openai",
        llm_base_url="https://api.deepseek.com",
        llm_api_key="provider-key",
        llm_model="deepseek-v4-flash",
        llm_extra_body={"user_id": "sag-local"},
    )

    request = apply_litellm_completion_policy(
        configured,
        {
            "model": configured.routed_llm_model,
            "messages": [],
            "tools": [{"type": "function", "function": {"name": "search_context"}}],
            "tool_choice": "required",
        },
    )

    assert request["extra_body"] == {
        "user_id": "sag-local",
        "thinking": {"type": "disabled"},
    }


def test_non_deepseek_openai_compatible_tool_request_is_unchanged():
    configured = Settings(
        _env_file=None,
        llm_provider="openai",
        llm_base_url="https://openai-compatible.example.com/v1",
        llm_api_key="provider-key",
        llm_model="custom-model",
    )
    raw = {
        "model": configured.routed_llm_model,
        "messages": [],
        "tools": [{"type": "function", "function": {"name": "search_context"}}],
        "tool_choice": "required",
    }

    request = apply_litellm_completion_policy(configured, raw)

    assert request == raw


@pytest.mark.asyncio
async def test_installed_litellm_policy_covers_dependency_owned_calls():
    import litellm

    configured = Settings(_env_file=None, llm_api_key="provider-key")
    previous_callbacks = list(litellm.callbacks)
    callback = install_litellm_policy(configured)
    try:
        request = await callback.async_pre_call_deployment_hook(
            {"model": "openai/qwen3.6-flash", "messages": []},
            SimpleNamespace(value="acompletion"),
        )
        assert request["reasoning_effort"] == "none"
        assert callback in litellm.callbacks
    finally:
        uninstall_litellm_policy(callback)
    assert litellm.callbacks == previous_callbacks


def test_document_output_redacts_database_details():
    from datetime import UTC, datetime

    from sag_api.enums import DocumentStatus
    from sag_api.schemas.document import DocumentOut

    payload = {
        "id": "doc-1",
        "source_id": "source-1",
        "filename": "note.md",
        "content_type": "text/markdown",
        "size_bytes": 12,
        "status": DocumentStatus.FAILED,
        "chunk_count": 0,
        "event_count": 0,
        "progress": 5,
        "token_usage": 0,
        "error": "(sqlite3.IntegrityError) FOREIGN KEY constraint failed [SQL: INSERT]",
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
    }
    document = DocumentOut.model_validate(payload)
    assert document.error == "信息源初始化未完成，文档尚未入库，请重试。"

    payload["error"] = "解析服务暂时不可用"
    document = DocumentOut.model_validate(payload)
    assert document.error == "解析服务暂时不可用"


@pytest.mark.asyncio
async def test_llm_timeout_and_retries_reach_unified_client(monkeypatch):
    from sag_api.generation import llm as generation_llm

    seen: dict = {}

    async def fake_completion(**kwargs):
        seen.update(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="pong"))])

    monkeypatch.setattr(generation_llm, "_litellm_completion", fake_completion)
    configured = Settings(
        _env_file=None,
        llm_api_key="provider-key",
        llm_timeout_ms=45_000,
        llm_max_retries=3,
    )

    client = generation_llm.LLMClient(configured)
    assert await client.complete([{"role": "user", "content": "ping"}]) == "pong"
    assert seen["model"] == "openai/qwen3.6-flash"
    assert seen["timeout"] == 45
    assert seen["num_retries"] == 3
    assert seen["reasoning_effort"] == "none"
    assert "reasoning_effort" in seen["allowed_openai_params"]
    assert "extra_body" not in seen

    engine = build_engine_config(configured)
    assert engine.llm.provider == "litellm"
    assert engine.llm.model == "openai/qwen3.6-flash"
    assert engine.llm.timeout == 45
    assert engine.llm.max_retries == 3


@pytest.mark.asyncio
async def test_deepseek_v4_agent_turn_sends_non_thinking_tool_request(monkeypatch):
    from sag_agent import AgentMessage, CancellationToken, ModelRequest
    from sag_api.generation import llm as generation_llm

    class EmptyStream:
        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

        async def aclose(self):
            return None

    seen: dict = {}

    async def fake_completion(**kwargs):
        seen.update(kwargs)
        return EmptyStream()

    monkeypatch.setattr(generation_llm, "_litellm_completion", fake_completion)
    client = generation_llm.LLMClient(
        Settings(
            _env_file=None,
            llm_provider="openai",
            llm_base_url="https://api.deepseek.com",
            llm_api_key="provider-key",
            llm_model="deepseek-v4-flash",
        )
    )
    request = ModelRequest(
        messages=(AgentMessage(role="user", content="你好"),),
        tools=(
            {
                "type": "function",
                "function": {
                    "name": "search_context",
                    "description": "search",
                    "parameters": {"type": "object"},
                },
            },
        ),
        tool_choice="none",
        turn=1,
    )

    chunks = [chunk async for chunk in client.stream_turn(request, CancellationToken())]

    assert chunks == []
    assert seen["model"] == "openai/deepseek-v4-flash"
    assert seen["tool_choice"] == "none"
    assert seen["extra_body"]["thinking"] == {"type": "disabled"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider", "model", "expected", "expected_temperature"),
    [
        ("openai", "qwen3.6-flash", "openai/qwen3.6-flash", 0.3),
        ("anthropic", "claude-sonnet-5", "anthropic/claude-sonnet-5", 1.0),
        ("gemini", "gemini/gemini-3.5-flash", "gemini/gemini-3.5-flash", 0.3),
    ],
)
async def test_generation_providers_use_one_litellm_route(monkeypatch, provider, model, expected, expected_temperature):
    from sag_api.generation import llm as generation_llm

    seen: dict = {}

    async def fake_completion(**kwargs):
        seen.update(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="pong"))])

    monkeypatch.setattr(generation_llm, "_litellm_completion", fake_completion)
    configured = Settings(
        _env_file=None,
        llm_provider=provider,
        llm_base_url=None,
        llm_api_key="provider-key",
        llm_model=model,
        llm_timeout_ms=45_000,
        llm_max_retries=3,
    )

    client = generation_llm.LLMClient(configured)
    assert await client.complete([{"role": "user", "content": "ping"}]) == "pong"
    assert seen["model"] == expected
    assert seen["api_key"] == "provider-key"
    assert seen["temperature"] == expected_temperature
    assert seen["timeout"] == 45
    assert seen["num_retries"] == 3
    assert "api_base" not in seen


@pytest.mark.asyncio
async def test_native_provider_stream_keeps_text_usage_and_tool_calls(monkeypatch):
    from sag_agent import AgentMessage, CancellationToken, ModelRequest
    from sag_api.generation import llm as generation_llm

    class ProviderStream:
        closed = False

        async def __aiter__(self):
            yield SimpleNamespace(
                usage={"prompt_tokens": 7, "completion_tokens": 3},
                choices=[],
            )
            yield SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        finish_reason=None,
                        delta=SimpleNamespace(content="先查询", tool_calls=[]),
                    )
                ]
            )
            yield SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        finish_reason="tool_calls",
                        delta=SimpleNamespace(
                            content=None,
                            tool_calls=[
                                SimpleNamespace(
                                    index=0,
                                    id="call-1",
                                    function=SimpleNamespace(
                                        name="search_context",
                                        arguments={"query": "SAG"},
                                    ),
                                )
                            ],
                        ),
                    )
                ]
            )

        async def close(self):
            self.closed = True

    stream = ProviderStream()
    seen: dict = {}

    async def fake_completion(**kwargs):
        seen.update(kwargs)
        return stream

    monkeypatch.setattr(generation_llm, "_litellm_completion", fake_completion)
    client = generation_llm.LLMClient(
        Settings(
            _env_file=None,
            llm_provider="gemini",
            llm_base_url=None,
            llm_api_key="gemini-key",
            llm_model="gemini-3.5-flash",
        )
    )
    request = ModelRequest(
        messages=(AgentMessage(role="user", content="查询 SAG"),),
        tools=(
            {
                "type": "function",
                "function": {
                    "name": "search_context",
                    "description": "search",
                    "parameters": {"type": "object"},
                },
            },
        ),
        tool_choice="required",
        turn=2,
    )

    chunks = [chunk async for chunk in client.stream_turn(request, CancellationToken())]
    assert seen["model"] == "gemini/gemini-3.5-flash"
    assert seen["tool_choice"] == "required"
    assert chunks[0].usage is not None and chunks[0].usage.total_tokens == 10
    assert chunks[1].text_delta == "先查询"
    assert chunks[-1].finish_reason == "tool_calls"
    assert chunks[-1].tool_calls[0].name == "search_context"
    assert chunks[-1].tool_calls[0].arguments == {"query": "SAG"}
    assert stream.closed is True


def test_native_generation_key_is_not_reused_for_openai_embeddings():
    configured = Settings(
        _env_file=None,
        llm_provider="anthropic",
        llm_api_key="anthropic-secret",
        llm_model="claude-sonnet-5",
        embedding_api_key=None,
    )

    assert configured.effective_embedding_api_key is None
    engine = build_engine_config(configured)
    assert engine.llm.provider == "litellm"
    assert engine.llm.model == "anthropic/claude-sonnet-5"
    assert engine.llm.temperature == 1.0
    assert engine.embedding.api_key == "not-configured"


def test_retrieved_section_from_dict():
    s = RetrievedSection.from_section({"chunk_id": "c1", "heading": "H", "content": "text", "score": 0.9, "rank": 2})
    assert s.chunk_id == "c1" and s.heading == "H" and s.score == 0.9 and s.rank == 2


def test_prompt_and_citations():
    sections = [
        RetrievedSection(
            chunk_id="c1",
            heading="创立",
            content="Acme 由张三创立。这是用于引用预览的补充正文。",
            score=0.8,
            rank=0,
            source_config_id="sc-1",
        )
    ]
    msgs = build_messages("谁创立了 Acme？", sections, language="zh")
    assert msgs[0]["role"] == "system"
    assert "Zleap" in msgs[0]["content"] and "你是 sag" not in msgs[0]["content"]
    assert "[1]" in msgs[-1]["content"] and "Acme" in msgs[-1]["content"]
    cites = build_citations(
        sections,
        events=[
            GraphEventInfo(
                id="event-1",
                source_id="doc-1",
                source_config_id="sc-1",
                chunk_id="c1",
                title="Acme 宣布创立",
                summary="张三完成了 Acme 的创立。",
                content="张三完成公司注册，并正式宣布 Acme 成立。",
                category="公司事件",
                start_time=datetime(2026, 7, 21, tzinfo=UTC),
            )
        ],
    )
    assert cites[0]["n"] == 1 and cites[0]["heading"] == "创立"
    assert cites[0]["snippet"] == "Acme 由张三创立。这是用于引用预览的补充正文。"
    assert "summary" not in cites[0]
    assert cites[0]["event_refs"] == [
        {
            "id": "event-1",
            "title": "Acme 宣布创立",
            "summary": "张三完成了 Acme 的创立。",
            "content": "张三完成公司注册，并正式宣布 Acme 成立。",
            "category": "公司事件",
            "start_time": "2026-07-21T00:00:00+00:00",
        }
    ]


def test_citation_events_use_source_and_chunk_composite_key_and_are_bounded():
    sections = [
        RetrievedSection(chunk_id="same", source_config_id="source-a", content="A"),
        RetrievedSection(chunk_id="same", source_config_id="source-b", content="B"),
    ]
    events = [
        GraphEventInfo(
            id=f"a-{index}",
            source_id="doc-a",
            source_config_id="source-a",
            chunk_id="same",
            title=f"A 事件 {index}",
        )
        for index in range(4)
    ] + [
        GraphEventInfo(
            id="b-1",
            source_id="doc-b",
            source_config_id="source-b",
            chunk_id="same",
            title="B 事件",
        )
    ]

    citations = build_citations(sections, events=events)

    assert [item["id"] for item in citations[0]["event_refs"]] == ["a-0", "a-1", "a-2"]
    assert [item["id"] for item in citations[1]["event_refs"]] == ["b-1"]
    assert citations[1]["event_refs"][0]["summary"] == ""
    assert citations[1]["event_refs"][0]["category"] == ""


@pytest.mark.asyncio
async def test_zleap_sag_extract_compat_repairs_missing_meta():
    from zleap.sag.modules.extract.processor import EventProcessor

    from sag_api.sag.compat import install_zleap_sag_extract_compat

    class FakeLLM:
        async def chat_with_schema(self, _messages, response_schema):
            data_schema = response_schema["properties"]["data"]
            meta_schema = data_schema["properties"]["meta"]
            assert "meta" not in data_schema.get("required", [])
            assert "reason" not in meta_schema.get("required", [])
            return {"type": "response", "data": {"items": []}}

    install_zleap_sag_extract_compat()

    schema = {
        "type": "object",
        "required": ["type", "data"],
        "properties": {
            "type": {"const": "response"},
            "data": {
                "type": "object",
                "required": ["items", "meta"],
                "properties": {
                    "items": {"type": "array"},
                    "meta": {
                        "type": "object",
                        "required": ["reason"],
                        "properties": {"reason": {"type": "string"}},
                    },
                },
            },
        },
    }
    fake_processor = SimpleNamespace(llm_client=FakeLLM())

    result = await EventProcessor._call_llm_with_retry(fake_processor, [], schema)

    assert result["data"]["items"] == []
    assert result["data"]["meta"]["reason"]


@pytest.mark.asyncio
async def test_zleap_sag_extract_compat_repairs_missing_is_valid():
    from zleap.sag.modules.extract.processor import EventProcessor

    from sag_api.sag.compat import install_zleap_sag_extract_compat

    class FakeLLM:
        async def chat_with_schema(self, _messages, response_schema):
            event_schema = response_schema["definitions"]["event"]
            assert "is_valid" not in event_schema.get("required", [])
            return {
                "type": "response",
                "data": {
                    "meta": {"reason": "ok"},
                    "items": [
                        {
                            "title": "顶层事项",
                            "content": "事项内容",
                            "references": [1],
                            "children": [
                                {
                                    "title": "子事项",
                                    "content": "子事项内容",
                                    "references": [1],
                                }
                            ],
                        }
                    ],
                },
            }

    install_zleap_sag_extract_compat()

    schema = {
        "type": "object",
        "required": ["type", "data"],
        "properties": {
            "type": {"const": "response"},
            "data": {
                "type": "object",
                "required": ["items", "meta"],
                "properties": {
                    "items": {"type": "array", "items": {"$ref": "#/definitions/event"}},
                    "meta": {
                        "type": "object",
                        "required": ["reason"],
                        "properties": {"reason": {"type": "string"}},
                    },
                },
            },
        },
        "definitions": {
            "event": {
                "type": "object",
                "required": ["title", "content", "references", "is_valid"],
                "properties": {
                    "title": {"type": "string"},
                    "content": {"type": "string"},
                    "references": {"type": "array", "items": {"type": "integer"}},
                    "is_valid": {"type": "boolean"},
                    "children": {"type": "array", "items": {"$ref": "#/definitions/event"}},
                },
            },
        },
    }
    fake_processor = SimpleNamespace(llm_client=FakeLLM())

    result = await EventProcessor._call_llm_with_retry(fake_processor, [], schema)

    item = result["data"]["items"][0]
    assert item["is_valid"] is True
    assert item["children"][0]["is_valid"] is True


@pytest.mark.asyncio
async def test_zleap_sag_extract_compat_repairs_empty_and_missing_references():
    from zleap.sag.modules.extract.processor import EventProcessor

    from sag_api.sag.compat import install_zleap_sag_extract_compat

    class FakeLLM:
        async def chat_with_schema(self, _messages, response_schema):
            event_schema = response_schema["definitions"]["event"]
            required = event_schema.get("required", [])
            # Soft fields must be dropped from required and lose the minItems floor.
            assert "references" not in required
            assert "title" not in required
            assert "content" not in required
            assert "minItems" not in event_schema["properties"]["references"]
            return {
                "type": "response",
                "data": {
                    "meta": {"reason": "ok"},
                    "items": [
                        # Model omitted references entirely and left title empty.
                        {"content": "有内容但缺少引用与标题"},
                        {
                            "title": "父事项",
                            "content": "父事项内容",
                            "references": [1],
                            "children": [
                                # Child returned references as empty list.
                                {"title": "子事项", "content": "子内容", "references": []}
                            ],
                        },
                    ],
                },
            }

    install_zleap_sag_extract_compat()

    schema = {
        "type": "object",
        "required": ["type", "data"],
        "properties": {
            "type": {"const": "response"},
            "data": {
                "type": "object",
                "required": ["items", "meta"],
                "properties": {
                    "items": {"type": "array", "items": {"$ref": "#/definitions/event"}},
                    "meta": {
                        "type": "object",
                        "required": ["reason"],
                        "properties": {"reason": {"type": "string"}},
                    },
                },
            },
        },
        "definitions": {
            "event": {
                "type": "object",
                "required": ["title", "content", "references", "is_valid"],
                "properties": {
                    "title": {"type": "string"},
                    "content": {"type": "string"},
                    "references": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "minItems": 1,
                    },
                    "is_valid": {"type": "boolean"},
                    "children": {"type": "array", "items": {"$ref": "#/definitions/event"}},
                },
            },
        },
    }
    fake_processor = SimpleNamespace(llm_client=FakeLLM())

    result = await EventProcessor._call_llm_with_retry(fake_processor, [], schema)

    first, second = result["data"]["items"]
    # Missing references/title backfilled to schema-shaped defaults; is_valid too.
    assert first["references"] == []
    assert first["title"] == ""
    assert first["is_valid"] is True
    # Non-empty references and provided fields are left untouched.
    assert second["references"] == [1]
    assert second["title"] == "父事项"
    assert second["children"][0]["references"] == []


@pytest.mark.asyncio
async def test_zleap_sag_extract_compat_falls_back_to_json_object_when_json_schema_is_unavailable():
    from zleap.sag.core.ai.base import BaseLLMClient
    from zleap.sag.core.ai.models import LLMMessage, LLMResponse, LLMRole
    from zleap.sag.modules.extract.processor import EventProcessor

    from sag_api.sag.compat import install_zleap_sag_extract_compat

    class JsonObjectOnlyClient(BaseLLMClient):
        def __init__(self):
            self.config = SimpleNamespace(
                model="moonshotai/kimi-k3",
                base_url="https://api.example.test/v1",
            )
            self.response_formats = []

        async def chat(self, _messages, **kwargs):
            response_format = kwargs.get("response_format")
            self.response_formats.append(response_format)
            if response_format and response_format.get("type") == "json_schema":
                raise RuntimeError("This response_format type is unavailable now")
            assert response_format == {"type": "json_object"}
            return LLMResponse(
                content='{"type":"response","data":{"items":[],"meta":{"reason":"ok"}}}',
                model="moonshotai/kimi-k3",
            )

        async def chat_stream(self, *_args, **_kwargs):
            if False:
                yield ""

    install_zleap_sag_extract_compat()
    schema = {
        "type": "object",
        "required": ["type", "data"],
        "properties": {
            "type": {"const": "response"},
            "data": {
                "type": "object",
                "required": ["items", "meta"],
                "properties": {
                    "items": {"type": "array"},
                    "meta": {
                        "type": "object",
                        "required": ["reason"],
                        "properties": {"reason": {"type": "string"}},
                    },
                },
            },
        },
    }
    client = JsonObjectOnlyClient()
    fake_processor = SimpleNamespace(llm_client=client)

    result = await EventProcessor._call_llm_with_retry(
        fake_processor,
        [LLMMessage(role=LLMRole.USER, content="extract")],
        schema,
    )

    assert result["data"]["meta"]["reason"] == "ok"
    assert [item["type"] for item in client.response_formats] == ["json_schema", "json_object"]


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["openai/deepseek-v4-flash", "openai/deepseek-v3"])
async def test_zleap_sag_extract_compat_uses_json_object_directly_for_deepseek(model):
    from zleap.sag.modules.extract.processor import EventProcessor

    from sag_api.sag.compat import install_zleap_sag_extract_compat

    class DeepSeekClient:
        def __init__(self):
            self.config = SimpleNamespace(model=model)
            self.calls = []

        async def chat_with_schema(self, _messages, response_schema, **kwargs):
            self.calls.append((response_schema, kwargs.get("response_format")))
            return {"type": "response", "data": {"items": [], "meta": {"reason": "ok"}}}

    install_zleap_sag_extract_compat()
    schema = {"type": "object", "properties": {"type": {"const": "response"}}}
    client = DeepSeekClient()

    result = await EventProcessor._call_llm_with_retry(SimpleNamespace(llm_client=client), [], schema)

    assert result["data"]["meta"]["reason"] == "ok"
    assert client.calls == [(None, {"type": "json_object"})]


def test_agent_name_is_injected_into_prompt():
    messages = build_agent_messages(
        "小跃",
        {"system_prompt": "保持严谨。"},
        "你叫什么？",
        language="zh",
    )
    system = messages[0]["content"]
    assert "你的名字是「小跃」" in system
    assert "保持严谨。" in system
    assert "sag" not in system.lower()
