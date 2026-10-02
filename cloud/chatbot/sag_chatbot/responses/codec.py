"""Translate only SAG's chat contract to Responses; no provider networking."""

from __future__ import annotations

import copy
from contextvars import ContextVar
from typing import Any

from .errors import provider_error

# Owned by one agent run, never shared between requests or persisted to the DB.
replay: ContextVar[dict | None] = ContextVar("sag_chatbot_responses_replay", default=None)


def invalid(message: str = "Malformed Responses payload"):
    return provider_error(422, message)


def content_parts(content: Any, role: str) -> list[dict]:
    kind = "output_text" if role == "assistant" else "input_text"
    if content is None:
        return []
    if isinstance(content, str):
        return [{"type": kind, "text": content}] if content else []
    if not isinstance(content, list):
        raise invalid("Unsupported message content for Responses")
    result = []
    for part in content:
        if part.get("type") == "text":
            result.append({"type": kind, "text": part["text"]})
        elif part.get("type") == "image_url" and role == "user":
            image = part["image_url"]
            result.append({"type": "input_image", "image_url": image["url"], "detail": image.get("detail", "auto")})
        else:
            raise invalid("Unsupported message content part for Responses")
    return result


def messages_input(messages: list[dict]) -> list[dict]:
    result = []
    state = replay.get()
    retained = set()
    for message in messages:
        role = message.get("role")
        if role == "tool":
            call_id = message.get("tool_call_id")
            if not call_id or not isinstance(message.get("content", ""), str):
                raise invalid("Responses tool output requires call ID and string content")
            result.append({"type": "function_call_output", "call_id": call_id, "output": message.get("content", "")})
            continue
        if role not in {"system", "developer", "user", "assistant"}:
            raise invalid("Unsupported message role for Responses")
        calls = message.get("tool_calls") or []
        key = tuple(call.get("id") for call in calls)
        if state is not None and key and key in state:
            old_message, output = state[key]
            # A context transform may change or remove an assistant message.
            if old_message == (message.get("content") or "", calls):
                result.extend(copy.deepcopy(output))
                retained.add(key)
                continue
        parts = content_parts(message.get("content"), role)
        if parts:
            result.append({"role": role, "content": parts})
        for call in calls:
            if call.get("type", "function") != "function" or not call.get("id"):
                raise invalid("Responses supports named function calls with IDs")
            function = call["function"]
            result.append(
                {
                    "type": "function_call",
                    "call_id": call["id"],
                    "name": function["name"],
                    "arguments": function["arguments"],
                }
            )
    if state is not None:
        for key in set(state) - retained:
            del state[key]
    return result


def request_body(model: str, messages: list[dict], params: dict, *, stream: bool, send_temperature: bool) -> dict:
    body = {"model": model, "input": messages_input(messages), "stream": stream, "store": False}
    if model == "" or not body["input"]:
        raise invalid("Responses requires a model and nonempty input")
    supported = {
        "max_tokens",
        "max_completion_tokens",
        "temperature",
        "top_p",
        "tools",
        "tool_choice",
        "response_format",
        "reasoning_effort",
        "parallel_tool_calls",
        "extra_body",
        "stream_options",
        "stream",
        "max_retries",
    }
    if set(params) - supported:
        raise invalid("Unsupported completion parameter for Responses; review the configured model options")
    maximum = params.get("max_completion_tokens", params.get("max_tokens"))
    if maximum is not None:
        body["max_output_tokens"] = maximum
    if send_temperature and params.get("temperature") is not None:
        body["temperature"] = params["temperature"]
    for name in ("top_p", "parallel_tool_calls"):
        if params.get(name) is not None:
            body[name] = params[name]
    if params.get("tools"):
        body["tools"] = []
        for tool in params["tools"]:
            if tool.get("type") != "function":
                raise invalid("Responses extension supports application function tools only")
            function = dict(tool["function"])
            # Responses otherwise defaults to stricter schemas than Chat Completions.
            function.setdefault("strict", False)
            body["tools"].append({"type": "function", **function})
    choice = params.get("tool_choice")
    if choice is not None:
        body["tool_choice"] = (
            {"type": "function", "name": choice["function"]["name"]} if isinstance(choice, dict) else choice
        )
    fmt = params.get("response_format")
    if fmt:
        if fmt.get("type") == "json_schema":
            body["text"] = {"format": {"type": "json_schema", **fmt["json_schema"]}}
        elif fmt.get("type") in {"json_object", "text"}:
            body["text"] = {"format": dict(fmt)}
        else:
            raise invalid("Unsupported response_format for Responses")
    effort = params.get("reasoning_effort")
    if effort is not None:
        body["reasoning"] = {"effort": effort}
    extra = params.get("extra_body") or {}
    if not isinstance(extra, dict) or set(extra) - {
        "reasoning",
        "text",
        "temperature",
        "top_p",
        "max_output_tokens",
        "truncation",
        "service_tier",
    }:
        raise invalid("Unsupported SAG_CHATBOT_LLM_EXTRA_BODY field for Responses; use Responses-native options")
    body.update(copy.deepcopy(extra))
    # Needed on APIs where encrypted reasoning is not included by default.
    body["include"] = ["reasoning.encrypted_content"]
    return body


def decode_response(response: dict) -> tuple[str, list[dict], dict]:
    status = response.get("status")
    if status != "completed":
        raise invalid("Responses generation incomplete or failed; no tool calls accepted")
    text = []
    calls = []
    ids = set()
    for item in response.get("output", []):
        kind = item.get("type")
        if kind == "message":
            for part in item.get("content", []):
                if part.get("type") == "refusal":
                    raise invalid("Responses model refused the request")
                if part.get("type") != "output_text":
                    raise invalid("Unsupported Responses output content")
                text.append(part["text"])
        elif kind == "function_call":
            if not item.get("call_id") or item["call_id"] in ids or not isinstance(item.get("arguments"), str):
                raise invalid("Malformed Responses function call")
            ids.add(item["call_id"])
            calls.append(
                {
                    "id": item["call_id"],
                    "type": "function",
                    "function": {"name": item["name"], "arguments": item["arguments"]},
                }
            )
        elif kind != "reasoning":
            raise invalid("Unsupported Responses output item; hosted tools are not enabled")
    if not text and not calls:
        raise invalid("Responses returned no text or function calls")
    raw = response.get("usage") or {}
    usage = {
        "prompt_tokens": raw.get("input_tokens", 0),
        "completion_tokens": raw.get("output_tokens", 0),
        "total_tokens": raw.get("total_tokens", raw.get("input_tokens", 0) + raw.get("output_tokens", 0)),
    }
    if raw.get("input_tokens_details"):
        usage["prompt_tokens_details"] = raw["input_tokens_details"]
    if raw.get("output_tokens_details"):
        usage["completion_tokens_details"] = raw["output_tokens_details"]
    answer = "".join(text)
    state = replay.get()
    if state is not None and calls:
        state[tuple(call["id"] for call in calls)] = ((answer, copy.deepcopy(calls)), copy.deepcopy(response["output"]))
    return answer, calls, usage
