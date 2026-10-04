"""Generation client with one optional connection snapshot per operation."""

from __future__ import annotations

from sag_agent import AgentRuntime
from sag_api.generation.llm import LLMClient
from sag_api.services import chatbot_service as rt


def sanitized_provider_error(exc):
    return rt.safe_failure("LLM", exc)


class SafeLLM(LLMClient):
    async def _create_completion(self, *args, **kwargs):
        query_token = rt.query_scope.set(True)
        try:
            response = await super()._create_completion(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 -- sanitize provider errors before stock logging.
            raise sanitized_provider_error(exc) from None
        finally:
            rt.query_scope.reset(query_token)
        if kwargs.get("stream"):
            return SafeStream(response)
        return response

    @staticmethod
    async def _close_stream(stream):
        await LLMClient._close_stream(stream)


class SafeStream:
    def __init__(self, stream):
        self.stream = stream
        self.iterator = stream.__aiter__()

    def __aiter__(self):
        return self

    async def __anext__(self):
        query_token = rt.query_scope.set(True)
        try:
            return await self.iterator.__anext__()
        except StopAsyncIteration:
            raise
        except Exception as exc:  # noqa: BLE001 -- streaming providers can raise arbitrary SDK errors.
            raise sanitized_provider_error(exc) from None
        finally:
            rt.query_scope.reset(query_token)

    async def aclose(self):
        try:
            await LLMClient._close_stream(self.stream)
        except Exception:  # noqa: BLE001 -- closing a provider stream must not expose credentials.
            raise sanitized_provider_error(Exception()) from None


class QueryLLM:
    """One scope per complete/stream; HTTP and Agent scopes keep tool turns coherent."""

    def __init__(self, settings):
        self._settings = settings

    @property
    def configured(self):
        snapshot = rt.operation.get() or rt.manager.snapshot()
        return snapshot.settings.llm_configured

    def client(self, snapshot):
        if not snapshot.connections["llm"].enabled:
            return LLMClient(snapshot.stock)
        return SafeLLM(snapshot.settings)

    async def complete(self, messages):
        async with rt.manager.scope() as snapshot:
            return await self.client(snapshot).complete(messages)

    async def stream_complete(self, messages):
        async with rt.manager.scope() as snapshot:
            try:
                stream = self.client(snapshot).stream_complete(messages)
                try:
                    async for chunk in stream:
                        yield chunk
                finally:
                    await stream.aclose()
            except Exception as exc:
                if snapshot.connections["llm"].enabled:
                    raise rt.safe_failure("LLM", exc) from None
                raise

    async def stream_turn(self, request, cancellation):
        async with rt.manager.scope() as snapshot:
            try:
                stream = self.client(snapshot).stream_turn(request, cancellation)
                try:
                    async for chunk in stream:
                        yield chunk
                finally:
                    await stream.aclose()
            except Exception as exc:
                if snapshot.connections["llm"].enabled:
                    raise rt.safe_failure("LLM", exc) from None
                raise


class QueryAgentRuntime(AgentRuntime):
    """Keep one snapshot alive across all model turns and nested retrieval tools."""

    async def _drive(self, agent, handle, tools):
        from sag_api.generation.responses.routing import run_replay

        state = {}
        token = run_replay.set(state)
        try:
            async with rt.manager.scope():
                await super()._drive(agent, handle, tools)
        finally:
            state.clear()
            run_replay.reset(token)

    async def _model_turn(self, *args, **kwargs):
        from sag_api.generation.responses.codec import replay
        from sag_api.generation.responses.routing import run_replay

        token = replay.set(run_replay.get())
        try:
            return await super()._model_turn(*args, **kwargs)
        finally:
            replay.reset(token)
