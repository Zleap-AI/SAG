"""Muse-wide LiteLLM request policy.

Generation calls can apply this policy directly.  zleap-sag calls LiteLLM
inside the dependency, so the application lifespan also installs the same
policy as a LiteLLM pre-call hook.  This keeps provider quirks in Muse without
patching ``site-packages``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from sag_api.core.config import Settings

_COMPLETION_CALL_TYPES = {"completion", "acompletion"}
_DEEPSEEK_V4_MODELS = {"deepseek-v4-flash", "deepseek-v4-pro"}


@dataclass(frozen=True)
class CapabilityKey:
    provider: str
    base_url: str
    model: str


class StructuredOutputCapabilityCache:
    def __init__(self) -> None:
        self._modes: dict[CapabilityKey, str] = {}
        self._locks: dict[CapabilityKey, asyncio.Lock] = {}

    def get(self, key: CapabilityKey) -> str | None:
        return self._modes.get(key)

    def mark(self, key: CapabilityKey, mode: str) -> None:
        self._modes[key] = mode

    def probe_lock(self, key: CapabilityKey) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock


def _capability_key(request: Mapping[str, Any], settings: Settings) -> CapabilityKey:
    model = str(request.get("model") or settings.routed_llm_model)
    explicit_provider = request.get("custom_llm_provider")
    provider = str(
        explicit_provider
        or (model.split("/", 1)[0] if "/" in model else settings.llm_provider)
    ).casefold()
    base_url = str(
        request.get("api_base")
        or request.get("base_url")
        or settings.llm_base_url
        or ""
    ).rstrip("/").casefold()
    return CapabilityKey(provider, base_url, model.casefold())


def _json_schema_request(request: Mapping[str, Any]) -> bool:
    response_format = request.get("response_format")
    return (
        isinstance(response_format, Mapping)
        and response_format.get("type") == "json_schema"
    )


def _with_json_object(request: Mapping[str, Any]) -> dict[str, Any]:
    changed = dict(request)
    changed["response_format"] = {"type": "json_object"}
    return changed


def _status_code(error: Exception) -> int | None:
    direct = getattr(error, "status_code", None)
    response = getattr(error, "response", None)
    value = direct if direct is not None else getattr(response, "status_code", None)
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def should_downgrade_json_schema(
    error: Exception,
    request: Mapping[str, Any],
    settings: Settings,
) -> bool:
    if settings.llm_structured_output_mode != "auto":
        return False
    if not _json_schema_request(request) or _status_code(error) not in {400, 422}:
        return False
    summary = str(error).casefold()
    names_schema = "json_schema" in summary or (
        "response_format" in summary and "schema" in summary
    )
    unsupported = any(
        phrase in summary
        for phrase in (
            "not supported",
            "unsupported",
            "does not support",
            "isn't supported",
            "not support",
        )
    )
    return names_schema and unsupported


def _thinking_override(extra_body: object) -> bool | None:
    if not isinstance(extra_body, Mapping):
        return None
    direct = extra_body.get("enable_thinking")
    if isinstance(direct, bool):
        return direct
    template_kwargs = extra_body.get("chat_template_kwargs")
    if isinstance(template_kwargs, Mapping):
        nested = template_kwargs.get("enable_thinking")
        if isinstance(nested, bool):
            return nested
    return None


def _is_openai_route(model: str, settings: Settings) -> bool:
    if "/" in model:
        return model.split("/", 1)[0].casefold() == "openai"
    return settings.llm_provider == "openai"


def _is_deepseek_v4(model: str) -> bool:
    model_id = model.rsplit("/", 1)[-1].casefold()
    return model_id in _DEEPSEEK_V4_MODELS


def _with_allowed_openai_param(request: dict[str, Any], name: str) -> None:
    configured = request.get("allowed_openai_params")
    if configured is None:
        allowed: list[str] = []
    elif isinstance(configured, str):
        allowed = [configured]
    else:
        allowed = list(configured)
    if name not in allowed:
        allowed.append(name)
    request["allowed_openai_params"] = allowed


def apply_litellm_completion_policy(
    settings: Settings,
    request: Mapping[str, Any],
) -> dict[str, Any]:
    """Return one normalized LiteLLM completion request.

    Qwen reasoning is disabled through LiteLLM's standard
    ``reasoning_effort`` argument.  ``allowed_openai_params`` is required for
    custom OpenAI-compatible model names whose capabilities LiteLLM cannot
    infer.  An explicit ``enable_thinking: true`` remains an opt-in override.
    """

    normalized = dict(request)
    if "extra_body" not in normalized and settings.llm_extra_body:
        normalized["extra_body"] = dict(settings.llm_extra_body)

    model = str(normalized.get("model") or settings.routed_llm_model)
    if _is_deepseek_v4(model):
        extra_body = dict(normalized.get("extra_body") or {})
        extra_body["thinking"] = {"type": "disabled"}
        normalized["extra_body"] = extra_body

    thinking = _thinking_override(normalized.get("extra_body"))
    if "reasoning_effort" not in normalized:
        if thinking is False or (thinking is None and "qwen" in model.casefold()):
            normalized["reasoning_effort"] = "none"

    if "reasoning_effort" in normalized and _is_openai_route(model, settings):
        _with_allowed_openai_param(normalized, "reasoning_effort")
    return normalized


def install_litellm_policy(settings: Settings) -> Any:
    """Install the Muse policy for dependency-owned LiteLLM calls."""

    import litellm
    from litellm.integrations.custom_logger import CustomLogger

    class MuseLiteLLMPolicy(CustomLogger):
        async def async_pre_call_deployment_hook(
            self,
            kwargs: dict[str, Any],
            call_type: Any,
        ) -> dict[str, Any]:
            kind = getattr(call_type, "value", call_type)
            if kind is not None and kind not in _COMPLETION_CALL_TYPES:
                return kwargs
            return apply_litellm_completion_policy(settings, kwargs)

    callback = MuseLiteLLMPolicy()
    litellm.callbacks.append(callback)
    original_acompletion = litellm.acompletion
    cache = StructuredOutputCapabilityCache()

    async def sag_acompletion(*args: Any, **kwargs: Any) -> Any:
        if settings.llm_structured_output_mode != "auto" or not _json_schema_request(
            kwargs
        ):
            return await original_acompletion(*args, **kwargs)
        key = _capability_key(kwargs, settings)
        known = cache.get(key)
        if known == "json_object":
            return await original_acompletion(*args, **_with_json_object(kwargs))
        if known == "json_schema":
            return await original_acompletion(*args, **kwargs)

        async with cache.probe_lock(key):
            known = cache.get(key)
            if known == "json_object":
                return await original_acompletion(*args, **_with_json_object(kwargs))
            if known == "json_schema":
                return await original_acompletion(*args, **kwargs)
            try:
                result = await original_acompletion(*args, **kwargs)
            except Exception as error:
                if not should_downgrade_json_schema(error, kwargs, settings):
                    raise
                cache.mark(key, "json_object")
                return await original_acompletion(
                    *args, **_with_json_object(kwargs)
                )
            cache.mark(key, "json_schema")
            return result

    callback._sag_original_acompletion = original_acompletion
    callback._sag_acompletion = sag_acompletion
    callback._sag_structured_output_cache = cache
    litellm.acompletion = sag_acompletion
    return callback


def uninstall_litellm_policy(callback: Any) -> None:
    """Remove a policy installed by :func:`install_litellm_policy`."""

    import litellm

    wrapper = getattr(callback, "_sag_acompletion", None)
    original = getattr(callback, "_sag_original_acompletion", None)
    if wrapper is not None and original is not None and litellm.acompletion is wrapper:
        litellm.acompletion = original
    if callback in litellm.callbacks:
        litellm.callbacks.remove(callback)
