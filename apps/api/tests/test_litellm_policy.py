from __future__ import annotations

import asyncio
from typing import Any

import litellm
import pytest

from sag_api.core.config import Settings
from sag_api.core.litellm_policy import install_litellm_policy, uninstall_litellm_policy


class GatewayError(RuntimeError):
    def __init__(self, message: str, status_code: int):
        super().__init__(message)
        self.status_code = status_code


def _schema_request(**overrides: Any) -> dict[str, Any]:
    return {
        "model": "openai/qwen-test",
        "api_base": "https://gateway.example/v1",
        "messages": [{"role": "user", "content": "extract"}],
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "answer", "schema": {"type": "object"}},
        },
        **overrides,
    }


@pytest.mark.asyncio
async def test_auto_downgrades_once_and_caches_by_provider_base_url_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    async def completion(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        if kwargs["response_format"]["type"] == "json_schema":
            raise GatewayError("response_format json_schema is not supported", 400)
        return {"ok": True}

    monkeypatch.setattr(litellm, "acompletion", completion)
    original = litellm.acompletion
    handle = install_litellm_policy(
        Settings(_env_file=None, llm_structured_output_mode="auto")
    )
    try:
        assert await litellm.acompletion(**_schema_request()) == {"ok": True}
        assert await litellm.acompletion(**_schema_request()) == {"ok": True}
        assert [call["response_format"]["type"] for call in calls] == [
            "json_schema",
            "json_object",
            "json_object",
        ]

        await litellm.acompletion(
            **_schema_request(api_base="https://another.example/v1")
        )
        assert calls[-2]["response_format"]["type"] == "json_schema"
    finally:
        uninstall_litellm_policy(handle)
    assert litellm.acompletion is original


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("message", "status"),
    [
        ("invalid request body", 400),
        ("response_format json_schema is not supported", 401),
        ("response_format json_schema rate limit", 429),
    ],
)
async def test_auto_never_downgrades_unrelated_or_non_capability_errors(
    monkeypatch: pytest.MonkeyPatch, message: str, status: int
) -> None:
    calls = 0

    async def completion(**_kwargs: Any) -> None:
        nonlocal calls
        calls += 1
        raise GatewayError(message, status)

    monkeypatch.setattr(litellm, "acompletion", completion)
    handle = install_litellm_policy(Settings(_env_file=None))
    try:
        with pytest.raises(GatewayError, match=message):
            await litellm.acompletion(**_schema_request())
    finally:
        uninstall_litellm_policy(handle)
    assert calls == 1


@pytest.mark.asyncio
async def test_auto_serializes_first_probe_for_same_capability_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    schema_calls = 0
    object_calls = 0

    async def completion(**kwargs: Any) -> dict[str, Any]:
        nonlocal schema_calls, object_calls
        if kwargs["response_format"]["type"] == "json_schema":
            schema_calls += 1
            await asyncio.sleep(0.01)
            raise GatewayError("json_schema response_format unsupported", 422)
        object_calls += 1
        return {"ok": True}

    monkeypatch.setattr(litellm, "acompletion", completion)
    handle = install_litellm_policy(Settings(_env_file=None))
    try:
        results = await asyncio.gather(
            litellm.acompletion(**_schema_request()),
            litellm.acompletion(**_schema_request()),
        )
    finally:
        uninstall_litellm_policy(handle)
    assert results == [{"ok": True}, {"ok": True}]
    assert schema_calls == 1
    assert object_calls == 2


@pytest.mark.asyncio
async def test_explicit_mode_does_not_auto_downgrade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    async def completion(**_kwargs: Any) -> None:
        nonlocal calls
        calls += 1
        raise GatewayError("response_format json_schema is not supported", 400)

    monkeypatch.setattr(litellm, "acompletion", completion)
    handle = install_litellm_policy(
        Settings(_env_file=None, llm_structured_output_mode="json_schema")
    )
    try:
        with pytest.raises(GatewayError):
            await litellm.acompletion(**_schema_request())
    finally:
        uninstall_litellm_policy(handle)
    assert calls == 1
