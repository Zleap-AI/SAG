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
    # 0.8.2:向量后端为显式 VectorConfig 家族;lancedb 由 EngineConfig 从 data_dir 派生
    assert cfg.vector is not None
    assert cfg.vector.provider == "lancedb"
    assert cfg.storage_mode == "normal"
    assert cfg.relational is not None
    assert cfg.relational.provider == "sqlite"  # 零基础设施:由 data_dir 派生 SQLite
    assert cfg.llm.model == settings.routed_llm_model
    assert cfg.llm.max_tokens == settings.llm_max_tokens
    assert cfg.llm.provider == "litellm"
    assert cfg.data_dir == settings.data_dir


@pytest.mark.parametrize(
    ("configured_dimensions", "expected_dimensions"),
    [(None, None), (1024, 1024)],
)
def test_engine_config_preserves_embedding_dimensions(configured_dimensions, expected_dimensions):
    configured = Settings(_env_file=None, embedding_dimensions=configured_dimensions)

    assert build_engine_config(configured).embedding.dimensions == expected_dimensions


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("configured_dimensions", "expected_request_dimensions"),
    [(None, None), (1024, 1024)],
)
async def test_embedding_request_respects_configured_dimensions(
    configured_dimensions,
    expected_request_dimensions,
    monkeypatch,
):
    from zleap.sag.core.ai.embedding import EmbeddingClient

    config = build_engine_config(Settings(_env_file=None, embedding_dimensions=configured_dimensions))
    embedding = EmbeddingClient(
        model=config.embedding.model,
        api_key=config.embedding.api_key,
        base_url=config.embedding.base_url,
        dimensions=config.embedding.dimensions,
        max_retries=0,
    )
    requests: list[dict] = []

    async def create(**request):
        requests.append(request)
        return SimpleNamespace()

    monkeypatch.setattr(embedding.client.embeddings, "create", create)

    await embedding._create_embeddings("test input", label="test")

    assert requests == [
        {
            "input": "test input",
            "model": config.embedding.model,
            **({"dimensions": expected_request_dimensions} if expected_request_dimensions is not None else {}),
        }
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["lancedb", "es", "pgvector", "oceanbase"])
async def test_unconfigured_embedding_defers_vector_schema_creation_to_first_real_vector(provider):
    from zleap.sag.core.storage.schema import prepare_vector_schema

    class VectorAdapter:
        def __init__(self) -> None:
            self.provider = provider
            self.created: list[tuple[str, int]] = []

        async def schema_object_names(self):
            return frozenset()

        async def validate_schema_object(self, _name, _dimensions):
            raise AssertionError("no schema object exists before the first vector")

        async def create_schema_object(self, name, dimensions):
            self.created.append((name, dimensions))

    config = build_engine_config(Settings(_env_file=None, embedding_dimensions=None))
    adapter = VectorAdapter()

    scan = await prepare_vector_schema(
        adapter,
        storage_mode="normal",
        dimensions=config.embedding.dimensions,
        create_missing=True,
    )

    assert adapter.created == []
    assert scan.missing_objects


def test_engine_config_presets_structured_output_mode_by_model():
    from zleap.sag.core.ai.structured import StructuredOutputMode

    deepseek = Settings(_env_file=None, llm_provider="openai", llm_model="deepseek-v4-flash", llm_api_key="k")
    assert build_engine_config(deepseek).llm.structured_output_mode == StructuredOutputMode.JSON_SCHEMA

    qwen = Settings(_env_file=None, llm_provider="openai", llm_model="qwen3.6-flash", llm_api_key="k")
    assert build_engine_config(qwen).llm.structured_output_mode == StructuredOutputMode.JSON_SCHEMA

    explicit = Settings(_env_file=None, llm_structured_output_mode="json_object")
    assert build_engine_config(explicit).llm.structured_output_mode == StructuredOutputMode.JSON_OBJECT

    prompt_only = Settings(_env_file=None, llm_structured_output_mode="prompt_only")
    assert build_engine_config(prompt_only).llm.structured_output_mode == StructuredOutputMode.PROMPT_ONLY


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

    payload["status"] = DocumentStatus.DELETE_FAILED
    payload["error"] = "(sqlalchemy.exc.OperationalError) database is locked [SQL: DELETE]"
    document = DocumentOut.model_validate(payload)
    assert document.error == "文档删除失败，请重试；若仍失败，请查看服务日志。"

    payload["error"] = "(sqlite3.IntegrityError) FOREIGN KEY constraint failed [SQL: DELETE]"
    document = DocumentOut.model_validate(payload)
    assert document.error == "文档删除失败，请重试；若仍失败，请查看服务日志。"


def test_document_output_marks_octx_documents_without_original_files():
    from datetime import UTC, datetime

    from sag_api.enums import DocumentStatus
    from sag_api.schemas.document import DocumentOut

    payload = {
        "id": "doc-1",
        "source_id": "source-1",
        "filename": "report.pdf",
        "content_type": "application/pdf",
        "size_bytes": 12,
        "status": DocumentStatus.READY,
        "chunk_count": 1,
        "event_count": 1,
        "progress": 100,
        "token_usage": 0,
        "error": None,
        "octx_installation_id": "installation-1",
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
    }

    imported = DocumentOut.model_validate(payload).model_dump()
    assert imported["original_file_available"] is False
    assert "octx_installation_id" not in imported

    payload["octx_installation_id"] = None
    uploaded = DocumentOut.model_validate(payload).model_dump()
    assert uploaded["original_file_available"] is True


@pytest.mark.asyncio
async def test_octx_document_preview_rejects_markdown_surrogate_as_original(monkeypatch):
    from types import SimpleNamespace

    from sag_api.api.v1 import documents as routes
    from sag_api.core.errors import NotFoundError

    imported = SimpleNamespace(octx_installation_id="installation-1")

    async def fake_source(*_args, **_kwargs):
        return SimpleNamespace(id="source-1")

    async def fake_document(*_args, **_kwargs):
        return imported

    monkeypatch.setattr(routes, "get_source", fake_source)
    monkeypatch.setattr(routes, "get_public_document", fake_document)

    with pytest.raises(NotFoundError, match="OCTX 数据包未包含原始文件"):
        await routes.get_preview("source-1", "doc-1", session=SimpleNamespace())


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


def test_document_delete_keeps_v1_success_contract():
    from sag_api.main import app

    operation = app.openapi()["paths"][
        "/api/v1/sources/{source_id}/documents/{document_id}"
    ]["delete"]

    assert "200" in operation["responses"]
    assert "202" not in operation["responses"]
    schema = operation["responses"]["200"]["content"]["application/json"]["schema"]
    assert schema["$ref"].endswith("/Ok")
