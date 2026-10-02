"""Generation facade and independent Responses route using the query codec/transport."""
from __future__ import annotations

from contextlib import contextmanager

from sag_api.generation.llm import LLMClient

from sag_chatbot.responses.provider import ResponsesProvider

from . import runtime as rt


class QueryResponsesProvider(ResponsesProvider):
    def _prepare(self, kwargs, stream):
        headers, body = super()._prepare(kwargs, stream)
        if not rt.operation.get().connections["llm"].api_key:
            headers.pop("Authorization", None)
            headers.pop("api-key", None)
        return headers, body


def sanitized_provider_error(exc):
    from sag_chatbot.responses.errors import provider_error
    status = getattr(exc, "status_code", None)
    return provider_error(status if isinstance(status, int) and 400 <= status <= 599 else 502,
                          "Chatbot LLM request failed; check endpoint, provider, model and credentials")


class SafeLLM(LLMClient):
    def __init__(self, settings, replay_state=None):
        super().__init__(settings)
        self.replay_state = replay_state

    async def _create_completion(self, *args, **kwargs):
        from sag_chatbot.responses.codec import replay
        query_token = rt.query_scope.set(True)
        replay_token = replay.set(self.replay_state)
        try:
            response = await super()._create_completion(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 -- sanitize provider errors before stock logging.
            raise sanitized_provider_error(exc) from None
        finally:
            replay.reset(replay_token)
            rt.query_scope.reset(query_token)
        if kwargs.get("stream"):
            return SafeStream(response, self.replay_state)
        return response

    @staticmethod
    async def _close_stream(stream):
        await LLMClient._close_stream(stream)


class SafeStream:
    def __init__(self, stream, replay_state=None):
        self.stream = stream
        self.iterator = stream.__aiter__()
        self.replay_state = replay_state

    def __aiter__(self):
        return self

    async def __anext__(self):
        from sag_chatbot.responses.codec import replay
        query_token = rt.query_scope.set(True)
        replay_token = replay.set(self.replay_state)
        try:
            return await self.iterator.__anext__()
        except StopAsyncIteration:
            raise
        except Exception as exc:  # noqa: BLE001 -- streaming providers can raise arbitrary SDK errors.
            raise sanitized_provider_error(exc) from None
        finally:
            replay.reset(replay_token)
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
        return (snapshot.stock if rt.stock_connection_test.get() else snapshot.settings).llm_configured

    def client(self, snapshot):
        if rt.stock_connection_test.get() or not snapshot.connections["llm"].enabled:
            return LLMClient(snapshot.stock)
        state = rt.chat_replay.get()
        return SafeLLM(snapshot.settings, (state if state is not None else snapshot.replay) if snapshot.responses_config else None)

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


def install_responses_route():
    import litellm
    from litellm import CustomLLM

    class ChatbotResponses(CustomLLM):
        def handler(self):
            snapshot = rt.operation.get()
            if snapshot is None or not rt.query_scope.get() or snapshot.responses_config is None:
                from sag_chatbot.responses.errors import provider_error
                raise provider_error(422, "Chatbot Responses requires an active query operation")
            if snapshot.responses_handler is None:
                snapshot.responses_handler = QueryResponsesProvider(snapshot.responses_config)
            return snapshot.responses_handler

        @staticmethod
        @contextmanager
        def replay_scope():
            from sag_chatbot.responses.codec import replay
            state = rt.chat_replay.get()
            token = replay.set(state if state is not None else rt.operation.get().replay)
            try:
                yield
            finally:
                replay.reset(token)

        async def acompletion(self, *args, **kwargs):
            handler = self.handler()
            with self.replay_scope():
                return await handler.acompletion(*args, **kwargs)

        async def astreaming(self, *args, **kwargs):
            stream = self.handler().astreaming(*args, **kwargs)
            try:
                while True:
                    with self.replay_scope():
                        try:
                            chunk = await stream.__anext__()
                        except StopAsyncIteration:
                            return
                    yield chunk
            finally:
                await stream.aclose()

        def completion(self, *args, **kwargs):
            handler = self.handler()
            with self.replay_scope():
                return handler.completion(*args, **kwargs)

        def streaming(self, *args, **kwargs):
            stream = self.handler().streaming(*args, **kwargs)
            try:
                while True:
                    with self.replay_scope():
                        try:
                            chunk = next(stream)
                        except StopIteration:
                            return
                    yield chunk
            finally:
                stream.close()

    if any(entry["provider"] == "sag_chatbot_responses" for entry in litellm.custom_provider_map):
        raise RuntimeError("LiteLLM provider sag_chatbot_responses is already registered")
    litellm.custom_provider_map.append({"provider": "sag_chatbot_responses", "custom_handler": ChatbotResponses()})
