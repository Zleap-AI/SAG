"""Muse-wide LiteLLM request policy.

Generation calls can apply this policy directly.  zleap-sag calls LiteLLM
inside the dependency, so the application lifespan also installs the same
policy as a LiteLLM pre-call hook.  This keeps provider quirks in Muse without
patching ``site-packages``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sag_api.core.config import Settings

_COMPLETION_CALL_TYPES = {"completion", "acompletion"}


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
    return callback


def uninstall_litellm_policy(callback: Any) -> None:
    """Remove a policy installed by :func:`install_litellm_policy`."""

    import litellm

    if callback in litellm.callbacks:
        litellm.callbacks.remove(callback)
