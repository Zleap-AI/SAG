"""Unified LLM client for generation, streaming, and Agent tool calls.

Every configured provider uses the same LiteLLM boundary. Provider-specific
model routing and capability rules live in ``core.model_providers``; this
adapter only translates the normalized response into sag_agent events.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import AsyncIterator
from typing import Any

from sag_agent import CancellationToken, ModelChunk, ModelRequest, Usage
from sag_agent import ToolCall as RuntimeToolCall
from sag_api.core.config import Settings
from sag_api.core.errors import ConfigurationError, UpstreamError
from sag_api.core.litellm_policy import apply_litellm_completion_policy
from sag_api.core.logging import get_logger

log = get_logger("generation")

Message = dict[str, Any]


async def _litellm_completion(**kwargs: Any) -> Any:
    """Import lazily so an unconfigured server can still start without provider work."""
    from litellm import acompletion

    return await acompletion(**kwargs)


def _attr(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


class LLMClient:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    @property
    def configured(self) -> bool:
        return self._settings.llm_configured

    def _ensure_configured(self) -> None:
        if not self.configured:
            raise ConfigurationError("尚未配置 LLM（SAG_LLM_PROVIDER / SAG_LLM_API_KEY / SAG_LLM_MODEL）")

    async def _create_completion(
        self,
        messages: list[Message],
        *,
        stream: bool = False,
        tools: list[dict] | None = None,
        tool_choice: str | dict | None = None,
    ) -> Any:
        request: dict[str, Any] = {
            "model": self._settings.routed_llm_model,
            "api_key": self._settings.llm_api_key,
            "timeout": self._settings.llm_timeout_ms / 1000,
            "num_retries": self._settings.llm_max_retries,
            "messages": messages,
            "temperature": self._settings.effective_llm_temperature,
            "max_tokens": self._settings.llm_max_tokens,
            "stream": stream,
        }
        if tools:
            request["tools"] = tools
            if tool_choice is not None:
                request["tool_choice"] = tool_choice
        if self._settings.llm_base_url:
            request["api_base"] = self._settings.llm_base_url
        request = apply_litellm_completion_policy(self._settings, request)
        return await _litellm_completion(**request)

    @staticmethod
    async def _close_stream(stream: Any) -> None:
        close = getattr(stream, "close", None) or getattr(stream, "aclose", None)
        if close is None:
            return
        result = close()
        if inspect.isawaitable(result):
            await result

    async def stream_turn(
        self,
        request: ModelRequest,
        cancellation: CancellationToken,
    ) -> AsyncIterator[ModelChunk]:
        """Stream one provider turn, including native function calls.

        A direct answer and a tool decision now share one provider request. This is
        the adapter required by sag_agent.ModelProvider.
        """

        self._ensure_configured()
        tool_parts: dict[int, dict[str, str]] = {}
        finish_reason: str | None = None
        stream = None
        try:
            stream = await self._create_completion(
                [message.to_model_dict() for message in request.messages],
                tools=list(request.tools) or None,
                tool_choice=request.tool_choice if request.tools else None,
                stream=True,
            )
            async for chunk in stream:
                cancellation.raise_if_cancelled()
                raw_usage = _attr(chunk, "usage")
                if raw_usage is not None:
                    prompt_details = _attr(raw_usage, "prompt_tokens_details")
                    completion_details = _attr(raw_usage, "completion_tokens_details")
                    yield ModelChunk(
                        usage=Usage(
                            input_tokens=int(_attr(raw_usage, "prompt_tokens", 0) or 0),
                            output_tokens=int(_attr(raw_usage, "completion_tokens", 0) or 0),
                            cached_tokens=int(_attr(prompt_details, "cached_tokens", 0) or 0),
                            reasoning_tokens=int(_attr(completion_details, "reasoning_tokens", 0) or 0),
                        )
                    )
                choices = _attr(chunk, "choices", []) or []
                if not choices:
                    continue
                choice = choices[0]
                finish_reason = _attr(choice, "finish_reason") or finish_reason
                delta = _attr(choice, "delta", {})
                token = _attr(delta, "content")
                if token:
                    yield ModelChunk(text_delta=token)
                for fallback_index, tool_delta in enumerate(_attr(delta, "tool_calls") or []):
                    index = _attr(tool_delta, "index")
                    index = fallback_index if index is None else int(index)
                    part = tool_parts.setdefault(index, {"id": "", "name": "", "arguments": ""})
                    tool_id = _attr(tool_delta, "id")
                    if tool_id:
                        part["id"] += str(tool_id)
                    function = _attr(tool_delta, "function")
                    if function is not None:
                        name = _attr(function, "name")
                        arguments = _attr(function, "arguments")
                        if name:
                            part["name"] += str(name)
                        if arguments:
                            part["arguments"] += (
                                arguments if isinstance(arguments, str) else json.dumps(arguments, ensure_ascii=False)
                            )

            calls: list[RuntimeToolCall] = []
            for index in sorted(tool_parts):
                part = tool_parts[index]
                raw_arguments = part["arguments"] or "{}"
                parse_error = None
                arguments: dict = {}
                try:
                    candidate = json.loads(raw_arguments)
                    if isinstance(candidate, dict):
                        arguments = candidate
                    else:
                        parse_error = "tool arguments must decode to an object"
                except (json.JSONDecodeError, TypeError) as exc:
                    parse_error = str(exc)
                calls.append(
                    RuntimeToolCall(
                        id=part["id"] or f"tool-{request.turn}-{index}",
                        name=part["name"],
                        arguments=arguments,
                        raw_arguments=raw_arguments,
                        parse_error=parse_error,
                    )
                )
            if calls or finish_reason:
                yield ModelChunk(tool_calls=tuple(calls), finish_reason=finish_reason)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.warning("LLM 轮次流式调用失败：%s", e)
            raise UpstreamError(f"生成失败：{e}") from e
        finally:
            if stream is not None:
                try:
                    await self._close_stream(stream)
                except Exception as e:  # noqa: BLE001
                    log.debug("LLM 轮次流关闭失败：%s", e)

    async def complete(self, messages: list[Message]) -> str:
        self._ensure_configured()
        try:
            resp = await self._create_completion(messages)
            choices = _attr(resp, "choices", []) or []
            if not choices:
                raise UpstreamError("模型未返回候选答案")
            return _attr(_attr(choices[0], "message", {}), "content", "") or ""
        except Exception as e:  # noqa: BLE001
            raise UpstreamError(f"生成失败：{e}") from e

    async def stream_complete(self, messages: list[Message]) -> AsyncIterator[str]:
        """Stream plain text completion deltas without the Agent/tool protocol."""

        self._ensure_configured()
        stream = None
        try:
            stream = await self._create_completion(messages, stream=True)
            async for chunk in stream:
                choices = _attr(chunk, "choices", []) or []
                if not choices:
                    continue
                token = _attr(_attr(choices[0], "delta", {}), "content")
                if token:
                    yield token
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            raise UpstreamError(f"生成失败：{e}") from e
        finally:
            # Closing explicitly makes browser aborts release the upstream HTTP
            # connection immediately, even when the stream is only partly read.
            if stream is not None:
                try:
                    await self._close_stream(stream)
                except Exception as e:  # noqa: BLE001
                    log.debug("LLM 流关闭失败：%s", e)
