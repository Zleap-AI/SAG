"""Validated connection drafts and protocol-aware inheritance. No database work."""
from __future__ import annotations

import json
import os
from urllib.parse import urlsplit

from cryptography.fernet import Fernet
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from sag_api.core.config import Settings
from sag_api.core.errors import ConfigurationError
from sag_api.core.model_providers import (
    _PROVIDER_SPECS,
    get_model_provider,
)

from sag_chatbot.responses.config import Config as ResponsesConfig
from sag_chatbot.responses.config import boolean, endpoint_url, load_thinking_rules

PROVIDERS = {spec.id: spec for spec in _PROVIDER_SPECS}
RESPONSES_PROVIDERS = ("openai", "azure", "bedrock_runtime", "bedrock_mantle")
# SDK factories require a nonempty token; never let a keyless query inherit SDK credentials.
KEYLESS_API_KEY = "sag-keyless-endpoint"
TUNING = (
    "temperature", "max_tokens", "context_window", "timeout_ms", "max_retries",
    "structured_output_mode", "extra_body",
)


def error(message: str):
    return ConfigurationError("Chatbot configuration: " + message)


def validate_url(value: str):
    if not value:
        return
    try:
        parsed = urlsplit(value)
        valid = parsed.scheme in {"https", "http"} and parsed.hostname and parsed.port != 0
        valid = valid and not (parsed.username or parsed.password or parsed.query or parsed.fragment)
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("Endpoint must be an HTTP(S) URL without credentials, query, or fragment")


class Draft(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class LLMConnection(Draft):
    enabled: bool = False
    provider: str = "openai"
    base_url: str = ""
    model: str = ""
    api_key: str = Field(default="", repr=False, max_length=8192)
    responses_provider: str = "openai"
    responses_endpoint: str = ""
    responses_api_version: str = ""

    @model_validator(mode="after")
    def validate_connection(self):
        if self.provider not in {*PROVIDERS, "responses"}:
            raise ValueError("Unsupported chatbot LLM provider")
        if self.responses_provider not in RESPONSES_PROVIDERS:
            raise ValueError("Unsupported chatbot Responses provider")
        validate_url(self.base_url)
        if any("\n" in v or "\r" in v for v in (self.model, self.api_key)):
            raise ValueError("Model and API key must be single-line values")
        if self.provider == "responses" and (self.enabled or self.responses_endpoint):
            self.responses_endpoint = endpoint_url(
                self.responses_endpoint, self.responses_provider, self.responses_api_version,
            )
        if self.enabled and not self.model.strip():
            raise ValueError("An enabled separate LLM requires its own model")
        return self

    def identity(self):
        if self.provider == "responses":
            return self.provider, self.responses_provider, self.responses_endpoint, self.responses_api_version
        return self.provider, self.base_url.rstrip("/")


class EmbeddingConnection(Draft):
    enabled: bool = False
    base_url: str = ""
    api_key: str = Field(default="", repr=False, max_length=8192)

    @model_validator(mode="after")
    def validate_connection(self):
        validate_url(self.base_url)
        if "\n" in self.api_key or "\r" in self.api_key:
            raise ValueError("API key must be a single-line value")
        if self.enabled and not self.base_url:
            raise ValueError("An enabled query embedding connection requires an endpoint")
        return self

    def identity(self):
        return (self.base_url.rstrip("/"),)


class LLMUpdate(Draft):
    enabled: bool | None = None
    provider: str | None = None
    base_url: str | None = None
    model: str | None = None
    api_key: str | None = Field(default=None, repr=False, max_length=8192)
    responses_provider: str | None = None
    responses_endpoint: str | None = None
    responses_api_version: str | None = None


class EmbeddingUpdate(Draft):
    enabled: bool | None = None
    base_url: str | None = None
    api_key: str | None = Field(default=None, repr=False, max_length=8192)


class Update(Draft):
    llm: LLMUpdate | None = None
    embedding: EmbeddingUpdate | None = None


class TestDraft(Update):
    target: str


class QuerySettings(Settings):
    chatbot_route: str = ""

    @property
    def llm_configured(self):
        return bool(self.chatbot_route) or super().llm_configured

    @property
    def routed_llm_model(self):
        if self.chatbot_route:
            prefix = self.chatbot_route + "/"
            return self.llm_model if self.llm_model.startswith(prefix) else prefix + self.llm_model
        return super().routed_llm_model

    @property
    def effective_llm_temperature(self):
        if self.chatbot_route:
            return PROVIDERS[self.llm_provider].resolve_temperature(self.llm_temperature)
        return super().effective_llm_temperature


class Environment:
    def __init__(self, env=None):
        self.env = dict(os.environ if env is None else env)
        self.locked = self.flag("SAG_LOCK_CHATBOT_CONFIG")
        for field in ("MODEL", "DIMENSIONS", "SCHEMA_DIMENSIONS", "REQUEST_DIMENSIONS"):
            if self.env.get("SAG_CHATBOT_EMBEDDING_" + field):
                raise error("Query embedding model and dimensions must come from the original embedding configuration")
        self.connections = {}
        for target, cls in (("llm", LLMConnection), ("embedding", EmbeddingConnection)):
            values = {}
            for field in cls.model_fields:
                name = f"SAG_CHATBOT_{target.upper()}_{field.upper()}"
                if name in self.env:
                    values[field] = self.flag(name) if field == "enabled" else self.env[name].strip()
            # Validate syntax now; credentials can be supplied by a saved UI row at startup.
            enabled = values.pop("enabled", False)
            try:
                connection = cls(**values)
            except (ValueError, ValidationError):
                raise error(f"Invalid {target} environment connection; check SAG_CHATBOT_{target.upper()}_* fields") from None
            self.connections[target] = {**connection.model_dump(), "enabled": enabled}
        self.tuning = {}
        for field in TUNING:
            name = "SAG_CHATBOT_LLM_" + field.upper()
            if name not in self.env or self.env[name] == "":
                continue
            value = self.env[name]
            try:
                if field in {"max_tokens", "context_window", "timeout_ms", "max_retries"}:
                    value = int(value)
                elif field == "temperature":
                    value = float(value)
                elif field == "extra_body":
                    value = json.loads(value)
                    if not isinstance(value, dict):
                        raise ValueError()
                self.tuning["llm_" + field] = value
            except ValueError:
                raise error(f"Invalid {name}") from None
        self.encryption_key = self.env.get("SAG_CHATBOT_CONFIG_ENCRYPTION_KEY", "")
        if self.encryption_key:
            try:
                Fernet(self.encryption_key.encode())
            except (ValueError, TypeError):
                raise error("SAG_CHATBOT_CONFIG_ENCRYPTION_KEY must be a valid Fernet key") from None
        try:
            self.send_temperature = self.flag("SAG_CHATBOT_LLM_RESPONSES_SEND_TEMPERATURE")
            self.thinking_rules = load_thinking_rules(self.env.get("SAG_CHATBOT_LLM_RESPONSES_THINKING_CONFIG"))
        except ValueError:
            raise error("Invalid chatbot Responses thinking configuration") from None

    def flag(self, name):
        try:
            return boolean(self.env.get(name, "false"), name)
        except ValueError:
            raise error(f"{name} must be true or false") from None

    def resolve(self, stock, connections):
        llm = connections["llm"]
        values = stock.model_dump()
        response_config = None
        if llm.enabled:
            spec = PROVIDERS[llm.provider] if llm.provider != "responses" else PROVIDERS["openai"]
            protocol = "openai_responses" if llm.provider == "responses" else spec.protocol
            original_protocol = get_model_provider(stock.llm_provider).protocol
            values.update(
                llm_provider=spec.id,
                llm_model=llm.model,
                llm_api_key=llm.api_key,
                llm_base_url=llm.base_url or None,
                llm_extra_body=stock.llm_extra_body if protocol == original_protocol else None,
                chatbot_route="sag_chatbot_responses" if llm.provider == "responses" else spec.litellm_prefix,
            )
            values.update(self.tuning)
            if llm.provider == "responses":
                allowed = {"reasoning", "text", "temperature", "top_p", "max_output_tokens", "truncation", "service_tier"}
                if set(values.get("llm_extra_body") or {}) - allowed:
                    raise error("Chatbot Responses extra_body must contain supported Responses-native options")
                model = llm.model.rsplit("/", 1)[-1].casefold()
                reasoning = (values.get("llm_extra_body") or {}).get("reasoning", {})
                if "qwen" in model and model not in self.thinking_rules and not (
                    isinstance(reasoning, dict) and isinstance(reasoning.get("effort"), str) and reasoning["effort"]
                ):
                    raise error("Set SAG_CHATBOT_LLM_EXTRA_BODY reasoning.effort or a chatbot Responses thinking rule for Qwen")
                response_config = ResponsesConfig(
                    llm.responses_provider, llm.responses_endpoint, self.send_temperature, self.thinking_rules,
                )
        try:
            active = QuerySettings.model_validate(values)
            if llm.enabled and (not 0 <= active.llm_temperature <= 2 or active.llm_max_tokens < 1 or active.llm_context_window < 1):
                raise ValueError()
        except (ValueError, ValidationError):
            raise error("Invalid inherited or overridden chatbot LLM tuning") from None
        return active, response_config
