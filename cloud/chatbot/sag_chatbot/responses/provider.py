"""LiteLLM custom provider with secret-free errors and operation-owned HTTP clients."""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import asynccontextmanager

import httpx
from litellm import CustomLLM, ModelResponse
from openai import APIError

from .codec import decode_response, invalid, request_body
from .config import Config
from .errors import provider_error


def response_error(response: httpx.Response):
    # Inspect only to retain stock's explicit schema capability downgrade. Never
    # include provider text, URLs, headers or bodies in the raised error.
    message = "Responses provider rejected request"
    if response.status_code in {400, 422}:
        try:
            error = response.json().get("error", {})
            detail = str(error.get("message", "")).lower()
            parameter = str(error.get("param", "")).lower()
            schema = "json_schema" in detail or parameter in {"text.format", "text.format.type", "response_format"}
            unsupported = any(
                word in detail for word in ("not supported", "unsupported", "does not support", "unavailable")
            )
            if schema and unsupported:
                message = "Responses json_schema unsupported"
        except (ValueError, AttributeError):
            pass
    return provider_error(response.status_code, message)


def chunk(text="", *, call=None, finished=False, usage=None):
    return {
        "text": text,
        "tool_use": call,
        "is_finished": finished,
        "finish_reason": "tool_calls" if call else "stop",
        "usage": usage,
        "index": 0,
    }


class ResponsesProvider(CustomLLM):
    def __init__(self, config: Config, *, async_transport=None, sync_transport=None):
        super().__init__()
        self.config = config
        self.async_transport = async_transport
        self.sync_transport = sync_transport

    def _prepare(self, kwargs, stream):
        key = kwargs.get("api_key")
        if not isinstance(key, str) or not key or "\n" in key or "\r" in key:
            raise provider_error(401, "Responses API key is missing or invalid")
        header = {"api-key": key} if self.config.provider == "azure" else {"Authorization": f"Bearer {key}"}
        header["Accept"] = "text/event-stream" if stream else "application/json"
        try:
            body = request_body(
                kwargs["model"],
                kwargs["messages"],
                kwargs.get("optional_params") or {},
                stream=stream,
                send_temperature=self.config.send_temperature,
            )
        except (KeyError, TypeError, ValueError, AttributeError):
            raise invalid("Invalid Responses request configuration or messages") from None
        return header, body

    @staticmethod
    def _retry_count(kwargs):
        # LiteLLM passes its SDK retry budget into custom-provider optional_params.
        # The engine sends zero here because it has its own retry wrapper.
        return max(0, min(10, int((kwargs.get("optional_params") or {}).get("max_retries", 0))))

    @staticmethod
    def _retryable(error, attempt, retries):
        return attempt < retries and getattr(error, "status_code", None) in {408, 429, 500, 502, 503, 504}

    async def acompletion(self, *args, **kwargs):
        retries = self._retry_count(kwargs)
        for attempt in range(retries + 1):
            try:
                return await self._acompletion_once(**kwargs)
            except APIError as error:
                if not self._retryable(error, attempt, retries):
                    raise
                await asyncio.sleep(min(0.25 * 2**attempt, 4))

    def completion(self, *args, **kwargs):
        retries = self._retry_count(kwargs)
        for attempt in range(retries + 1):
            try:
                return self._completion_once(**kwargs)
            except APIError as error:
                if not self._retryable(error, attempt, retries):
                    raise
                time.sleep(min(0.25 * 2**attempt, 4))

    async def astreaming(self, *args, **kwargs):
        retries = self._retry_count(kwargs)
        for attempt in range(retries + 1):
            emitted = False
            stream = self._astreaming_once(**kwargs)
            try:
                async for item in stream:
                    emitted = True
                    yield item
                return
            except APIError as error:
                if emitted or not self._retryable(error, attempt, retries):
                    raise
            finally:
                await stream.aclose()
            await asyncio.sleep(min(0.25 * 2**attempt, 4))

    def streaming(self, *args, **kwargs):
        retries = self._retry_count(kwargs)
        for attempt in range(retries + 1):
            emitted = False
            stream = self._streaming_once(**kwargs)
            try:
                for item in stream:
                    emitted = True
                    yield item
                return
            except APIError as error:
                if emitted or not self._retryable(error, attempt, retries):
                    raise
            finally:
                stream.close()
            time.sleep(min(0.25 * 2**attempt, 4))

    @asynccontextmanager
    async def _client(self, kwargs):
        # No hidden HTTPX retries; the explicit provider loop owns this budget.
        try:
            async with httpx.AsyncClient(
                timeout=kwargs.get("timeout") or 60, transport=self.async_transport, follow_redirects=False
            ) as client:
                yield client
        except httpx.TimeoutException:
            raise provider_error(408, "Responses provider timed out") from None
        except httpx.HTTPError:
            raise provider_error(503, "Responses provider connection failed") from None
        except (KeyError, TypeError, ValueError, AttributeError):
            raise invalid() from None

    @staticmethod
    def _result(response, model):
        text, calls, usage = decode_response(response)
        return ModelResponse(
            id=response.get("id"),
            model=model,
            choices=[
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": text, "tool_calls": calls or None},
                    "finish_reason": "tool_calls" if calls else "stop",
                }
            ],
            usage=usage,
        )

    async def _acompletion_once(self, **kwargs):
        headers, body = self._prepare(kwargs, False)
        async with self._client(kwargs) as client:
            response = await client.post(self.config.endpoint, headers=headers, json=body)
            if response.status_code != 200:
                raise response_error(response)
            return self._result(response.json(), kwargs["model"])

    def _completion_once(self, **kwargs):
        headers, body = self._prepare(kwargs, False)
        try:
            with httpx.Client(
                timeout=kwargs.get("timeout") or 60, transport=self.sync_transport, follow_redirects=False
            ) as client:
                response = client.post(self.config.endpoint, headers=headers, json=body)
                if response.status_code != 200:
                    raise response_error(response)
                return self._result(response.json(), kwargs["model"])
        except httpx.TimeoutException:
            raise provider_error(408, "Responses provider timed out") from None
        except httpx.HTTPError:
            raise provider_error(503, "Responses provider connection failed") from None
        except (KeyError, TypeError, ValueError, AttributeError):
            raise invalid() from None

    async def _astreaming_once(self, **kwargs):
        headers, body = self._prepare(kwargs, True)
        async with (
            self._client(kwargs) as client,
            client.stream("POST", self.config.endpoint, headers=headers, json=body) as response,
        ):
            if response.status_code != 200:
                await response.aread()
                raise response_error(response)
            if response.headers.get("content-type", "").split(";", 1)[0] != "text/event-stream":
                raise invalid("Responses streaming requires text/event-stream")
            decoder = StreamDecoder()
            async for line in response.aiter_lines():
                for item in decoder.line(line):
                    yield item
                if decoder.completed:
                    return
            raise provider_error(502, "Responses stream ended before response.completed")

    def _streaming_once(self, **kwargs):
        headers, body = self._prepare(kwargs, True)
        try:
            with (
                httpx.Client(
                    timeout=kwargs.get("timeout") or 60, transport=self.sync_transport, follow_redirects=False
                ) as client,
                client.stream("POST", self.config.endpoint, headers=headers, json=body) as response,
            ):
                if response.status_code != 200:
                    response.read()
                    raise response_error(response)
                if response.headers.get("content-type", "").split(";", 1)[0] != "text/event-stream":
                    raise invalid("Responses streaming requires text/event-stream")
                decoder = StreamDecoder()
                for line in response.iter_lines():
                    yield from decoder.line(line)
                    if decoder.completed:
                        return
                raise provider_error(502, "Responses stream ended before response.completed")
        except httpx.TimeoutException:
            raise provider_error(408, "Responses provider timed out") from None
        except httpx.HTTPError:
            raise provider_error(503, "Responses provider connection failed") from None
        except (KeyError, TypeError, ValueError, AttributeError):
            raise invalid() from None


class StreamDecoder:
    """SSE frames may span HTTP chunks and contain multiple data lines."""

    def __init__(self):
        self.data = []
        self.size = 0
        self.text = ""
        self.completed = False

    def line(self, line):
        if line.startswith("data:"):
            self.data.append(line[5:].lstrip(" "))
            self.size += len(line)
            if self.size > 16 * 1024 * 1024:
                raise invalid("Responses SSE event exceeds 16 MiB")
            return []
        if line or not self.data:
            return []
        data = "\n".join(self.data)
        self.data, self.size = [], 0
        if data == "[DONE]":
            raise invalid("Responses stream terminated without response.completed")
        event = json.loads(data)
        kind = event.get("type")
        if kind == "response.output_text.delta":
            self.text += event["delta"]
            return [chunk(event["delta"])]
        if kind in {"error", "response.failed", "response.incomplete"}:
            raise invalid("Responses stream reported failure or incomplete generation")
        if kind == "response.refusal.delta":
            raise invalid("Responses model refused the request")
        if kind != "response.completed":
            return []
        text, calls, usage = decode_response(event["response"])
        if self.text and self.text != text:
            raise invalid("Responses completed text does not match streamed text")
        result = [chunk(text)] if text and not self.text else []
        # Only complete, successful tool calls reach SAG's execution layer. The
        # completed response is authoritative even when argument deltas fragment.
        for index, call in enumerate(calls):
            result.append(chunk(call={"index": index, **call}))
        final = chunk(finished=True, usage=usage)
        final["finish_reason"] = "tool_calls" if calls else "stop"
        result.append(final)
        self.completed = True
        return result
