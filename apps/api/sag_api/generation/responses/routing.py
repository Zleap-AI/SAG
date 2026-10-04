"""Native LiteLLM provider registration and request-local configuration."""

from contextvars import ContextVar

from litellm import CustomLLM

from sag_api.core.responses import Config

from .provider import ResponsesProvider

request_settings = ContextVar("sag_responses_settings", default=None)
run_replay = ContextVar("sag_responses_run_replay", default=None)


class NativeResponsesProvider(CustomLLM):
    def __init__(self, *, async_transport=None, sync_transport=None):
        super().__init__()
        self.async_transport = async_transport
        self.sync_transport = sync_transport

    def handler(self):
        from sag_api.core.config import settings
        from sag_api.services.chatbot_service import operation, query_scope

        active = request_settings.get()
        snapshot = operation.get()
        if active is None:
            active = snapshot.settings if snapshot is not None and query_scope.get() else settings
        keyless = (
            snapshot is not None
            and query_scope.get()
            and snapshot.connections["llm"].enabled
            and not (snapshot.connections["llm"].api_key)
        )
        return ResponsesProvider(
            Config.from_settings(active),
            async_transport=self.async_transport,
            sync_transport=self.sync_transport,
            omit_auth=keyless,
        )

    async def acompletion(self, *args, **kwargs):
        return await self.handler().acompletion(*args, **kwargs)

    async def astreaming(self, *args, **kwargs):
        stream = self.handler().astreaming(*args, **kwargs)
        try:
            async for chunk in stream:
                yield chunk
        finally:
            await stream.aclose()

    def completion(self, *args, **kwargs):
        return self.handler().completion(*args, **kwargs)

    def streaming(self, *args, **kwargs):
        yield from self.handler().streaming(*args, **kwargs)


def register():
    """Use LiteLLM's supported custom-provider API; registration is idempotent."""
    import litellm

    if not any(entry["provider"] == "sag_responses" for entry in litellm.custom_provider_map):
        litellm.custom_provider_map.append({"provider": "sag_responses", "custom_handler": NativeResponsesProvider()})


class ConfiguredStream:
    """Keep draft configuration on lazy stream reads without sharing it across tasks."""

    def __init__(self, stream, settings):
        self.stream = stream
        self.iterator = stream.__aiter__()
        self.settings = settings

    def __aiter__(self):
        return self

    async def __anext__(self):
        token = request_settings.set(self.settings)
        try:
            return await self.iterator.__anext__()
        finally:
            request_settings.reset(token)

    async def aclose(self):
        from sag_api.generation.llm import LLMClient

        await LLMClient._close_stream(self.stream)
