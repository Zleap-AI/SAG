"""Validated Responses endpoint and model reasoning configuration."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from types import MappingProxyType
from typing import Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

ResponsesProviderId = Literal["openai", "azure", "bedrock_runtime", "bedrock_mantle"]


@lru_cache(maxsize=16)
def load_thinking_rules(path=None):
    """Load one complete, immutable model policy at startup; never fall back."""
    source = Path(__file__).with_name("responses_thinking.json")
    if path:
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
            raise ValueError()
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
            "Responses configuration: cannot load model thinking rules; check SAG_LLM_RESPONSES_THINKING_CONFIG "
            "or responses_thinking.json (exact lowercase model IDs, default_effort, optional legacy_disabled_field)"
        ) from None
    return MappingProxyType({model: MappingProxyType(rule) for model, rule in rules.items()})


def endpoint_url(endpoint: str, provider: str, version: str = "") -> str:
    try:
        parsed = urlsplit(endpoint)
        valid = bool(parsed.hostname) and parsed.port != 0
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("Responses configuration: invalid SAG_LLM_RESPONSES_ENDPOINT")
    if parsed.username or parsed.password or parsed.fragment:
        raise ValueError("Responses endpoint cannot contain credentials or a fragment")
    if parsed.scheme != "https" and not (
        parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    ):
        raise ValueError("Responses endpoint requires HTTPS (HTTP is allowed only on loopback for tests)")
    path = parsed.path.rstrip("/")
    if not path.endswith("/responses"):
        raise ValueError("SAG_LLM_RESPONSES_ENDPOINT must be the full endpoint ending in /responses")
    query = parse_qsl(parsed.query, keep_blank_values=True)
    versions = [v for k, v in query if k == "api-version"]
    if len(versions) > 1 or (versions and not versions[0]):
        raise ValueError("Responses endpoint has duplicate or empty api-version")
    if provider == "azure":
        if not path.endswith(("/openai/responses", "/openai/v1/responses")):
            raise ValueError("Azure requires /openai/responses or /openai/v1/responses")
        if versions and version and versions[0] != version:
            raise ValueError("Azure endpoint and SAG_LLM_RESPONSES_API_VERSION conflict")
        effective = version or (versions[0] if versions else "")
        if not effective and path.endswith("/openai/v1/responses"):
            effective = "v1"
        if not effective:
            raise ValueError("Azure versioned Responses requires SAG_LLM_RESPONSES_API_VERSION or an api-version query")
        query = [(k, v) for k, v in query if k != "api-version"]
        query.append(("api-version", effective))
    elif version:
        raise ValueError("SAG_LLM_RESPONSES_API_VERSION is only supported for Azure")
    # Saved Bedrock service IDs remain compatible Bearer connections. The full
    # endpoint selects Runtime, Mantle, or a gateway; no hostname inference.
    return urlunsplit((parsed.scheme, parsed.netloc, path, urlencode(query), ""))


@dataclass(frozen=True)
class Config:
    provider: str = "disabled"
    endpoint: str = ""
    send_temperature: bool = False
    thinking_rules: Mapping[str, Mapping[str, str]] = field(default_factory=lambda: MappingProxyType({}))

    @classmethod
    def from_settings(cls, settings):
        if not settings.llm_model.strip() or any(
            "\n" in value or "\r" in value for value in (settings.llm_model, settings.llm_api_key or "")
        ):
            raise ValueError("Responses requires a nonempty model and single-line model/API key values")
        provider = settings.llm_responses_provider
        endpoint = endpoint_url(settings.llm_responses_endpoint, provider, settings.llm_responses_api_version)
        extra = settings.llm_extra_body or {}
        allowed = {"reasoning", "text", "temperature", "top_p", "max_output_tokens", "truncation", "service_tier"}
        if not isinstance(extra, dict) or set(extra) - allowed:
            raise ValueError("Responses extra_body requires supported Responses-native options")
        rules = load_thinking_rules(settings.llm_responses_thinking_config)
        model = settings.llm_model.rsplit("/", 1)[-1].casefold()
        reasoning = extra.get("reasoning", {})
        if (
            "qwen" in model
            and model not in rules
            and not (isinstance(reasoning, dict) and isinstance(reasoning.get("effort"), str) and reasoning["effort"])
        ):
            raise ValueError(
                "Responses cannot infer thinking-off support for this Qwen model; configure reasoning.effort"
            )
        return cls(provider, endpoint, settings.llm_responses_send_temperature, rules)
