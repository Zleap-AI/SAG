"""Single source of truth for generation-model provider capabilities.

The registry contains technical defaults and protocol behavior only. Runtime,
knowledge extraction, API configuration, and the web settings form all consume
the same catalog so adding a provider does not create another call path.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from types import MappingProxyType
from typing import Literal

ModelProviderId = Literal["openai", "anthropic", "gemini", "orcarouter"]


@dataclass(frozen=True, slots=True)
class ModelProviderSpec:
    id: ModelProviderId
    display_name: str
    protocol: str
    litellm_prefix: str
    default_model: str
    default_base_url: str | None
    default_context_window: int
    default_temperature: float
    temperature_configurable: bool
    can_reuse_embedding_credentials: bool
    api_key_placeholder: str

    def route_model(self, model: str) -> str:
        normalized = model.strip()
        prefix = f"{self.litellm_prefix}/"
        return normalized if normalized.startswith(prefix) else f"{prefix}{normalized}"

    def resolve_temperature(self, configured: float) -> float:
        return configured if self.temperature_configurable else self.default_temperature

    def to_public_dict(self) -> dict[str, object]:
        value = asdict(self)
        value.pop("litellm_prefix")
        return value


_PROVIDER_SPECS = (
    ModelProviderSpec(
        id="openai",
        display_name="OpenAI-compatible",
        protocol="openai_chat_completions",
        litellm_prefix="openai",
        default_model="qwen3.6-flash",
        default_base_url="https://api.302ai.cn/v1",
        default_context_window=128_000,
        default_temperature=0.3,
        temperature_configurable=True,
        can_reuse_embedding_credentials=True,
        api_key_placeholder="sk-…",
    ),
    ModelProviderSpec(
        id="anthropic",
        display_name="Anthropic",
        protocol="anthropic_messages",
        litellm_prefix="anthropic",
        default_model="claude-sonnet-5",
        default_base_url=None,
        default_context_window=1_000_000,
        default_temperature=1.0,
        temperature_configurable=False,
        can_reuse_embedding_credentials=False,
        api_key_placeholder="sk-ant-…",
    ),
    ModelProviderSpec(
        id="gemini",
        display_name="Google Gemini",
        protocol="gemini_generate_content",
        litellm_prefix="gemini",
        default_model="gemini-3.5-flash",
        default_base_url=None,
        default_context_window=1_048_576,
        default_temperature=0.3,
        temperature_configurable=True,
        can_reuse_embedding_credentials=False,
        api_key_placeholder="AIza…",
    ),
    # OrcaRouter is an OpenAI-compatible gateway that requires namespaced model
    # ids (e.g. "openai/gpt-4o-mini"). LiteLLM strips exactly one transport
    # prefix before forwarding, so a plain "openai" prefix would reduce the id
    # to a bare model name that OrcaRouter rejects. hosted_vllm is LiteLLM's
    # generic OpenAI-compatible passthrough: it strips only its own prefix and
    # forwards the remaining namespaced id unchanged.
    ModelProviderSpec(
        id="orcarouter",
        display_name="OrcaRouter",
        protocol="openai_chat_completions",
        litellm_prefix="hosted_vllm",
        default_model="openai/gpt-4o-mini",
        default_base_url="https://api.orcarouter.ai/v1",
        default_context_window=128_000,
        default_temperature=0.3,
        temperature_configurable=True,
        can_reuse_embedding_credentials=False,
        api_key_placeholder="sk-orca-…",
    ),
)

MODEL_PROVIDERS = MappingProxyType({spec.id: spec for spec in _PROVIDER_SPECS})


def get_model_provider(provider: ModelProviderId) -> ModelProviderSpec:
    return MODEL_PROVIDERS[provider]


def model_provider_catalog() -> list[dict[str, object]]:
    return [spec.to_public_dict() for spec in _PROVIDER_SPECS]
