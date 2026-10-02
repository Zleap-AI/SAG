"""Deployment configuration, independent of stock SAG imports."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


def load_thinking_rules(path=None):
    """Load one complete, immutable model policy at startup; never fall back."""
    source = Path(__file__).with_name("model_thinking.json")
    if path is not None:
        source = source.parent / Path(path)

    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate thinking configuration key")
            result[key] = value
        return result

    try:
        rules = json.loads(source.read_text(), object_pairs_hook=unique_object)
        if not isinstance(rules, dict):
            raise ValueError()  # noqa: TRY004 -- malformed policy data uses one configuration error path.
        for model, rule in rules.items():
            if not model or model != model.strip().casefold() or "/" in model or not model.isprintable():
                raise ValueError()
            if not isinstance(rule, dict) or set(rule) - {"default_effort", "legacy_disabled_field"}:
                raise ValueError()
            effort = rule.get("default_effort")
            if not isinstance(effort, str) or not effort or effort != effort.strip() or not effort.isprintable():
                raise ValueError()
            if "legacy_disabled_field" in rule and rule["legacy_disabled_field"] not in ("thinking", "enable_thinking"):
                raise ValueError()
    except (OSError, ValueError):
        raise ValueError(
            "Responses configuration: cannot load model thinking rules; check SAG_CHATBOT_LLM_RESPONSES_THINKING_CONFIG "
            "or model_thinking.json (exact lowercase model IDs, default_effort, optional legacy_disabled_field)"
        ) from None
    return MappingProxyType({model: MappingProxyType(rule) for model, rule in rules.items()})


def boolean(value: str, name: str) -> bool:
    if value.lower() not in {"true", "false", "1", "0"}:
        raise ValueError(f"Responses configuration: {name} must be true or false")
    return value.lower() in {"true", "1"}


def endpoint_url(endpoint: str, provider: str, version: str = "") -> str:
    try:
        parsed = urlsplit(endpoint)
        valid = bool(parsed.hostname) and parsed.port != 0
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("Responses configuration: invalid SAG_CHATBOT_LLM_RESPONSES_ENDPOINT")
    if parsed.username or parsed.password or parsed.fragment:
        raise ValueError("Responses endpoint cannot contain credentials or a fragment")
    if parsed.scheme != "https" and not (
        parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    ):
        raise ValueError("Responses endpoint requires HTTPS (HTTP is allowed only on loopback for tests)")
    path = parsed.path.rstrip("/")
    if not path.endswith("/responses"):
        raise ValueError("SAG_CHATBOT_LLM_RESPONSES_ENDPOINT must be the full endpoint ending in /responses")
    query = parse_qsl(parsed.query, keep_blank_values=True)
    versions = [v for k, v in query if k == "api-version"]
    if len(versions) > 1 or (versions and not versions[0]):
        raise ValueError("Responses endpoint has duplicate or empty api-version")
    if provider == "azure":
        if not path.endswith(("/openai/responses", "/openai/v1/responses")):
            raise ValueError("Azure requires /openai/responses or /openai/v1/responses")
        if versions and version and versions[0] != version:
            raise ValueError("Azure endpoint and SAG_CHATBOT_LLM_RESPONSES_API_VERSION conflict")
        effective = version or (versions[0] if versions else "")
        if not effective and path.endswith("/openai/v1/responses"):
            effective = "v1"
        if not effective:
            raise ValueError("Azure versioned Responses requires SAG_CHATBOT_LLM_RESPONSES_API_VERSION or an api-version query")
        query = [(k, v) for k, v in query if k != "api-version"]
        query.append(("api-version", effective))
    elif version:
        raise ValueError("SAG_CHATBOT_LLM_RESPONSES_API_VERSION is only supported for Azure")
    if provider.startswith("bedrock_"):
        expected = "/openai/v1/responses" if provider == "bedrock_runtime" else "/v1/responses"
        if path != expected:
            raise ValueError(f"{provider} requires endpoint path {expected}")
        host = parsed.hostname or ""
        if host.endswith(("amazonaws.com", "amazonaws.com.cn", "api.aws")):
            prefix = "bedrock-runtime." if provider == "bedrock_runtime" else "bedrock-mantle."
            if not host.startswith(prefix):
                raise ValueError("Bedrock endpoint host does not match SAG_CHATBOT_LLM_RESPONSES_PROVIDER")
    return urlunsplit((parsed.scheme, parsed.netloc, path, urlencode(query), ""))


@dataclass(frozen=True)
class Config:
    provider: str = "disabled"
    endpoint: str = ""
    send_temperature: bool = False
    thinking_rules: Mapping[str, Mapping[str, str]] = field(default_factory=lambda: MappingProxyType({}))
