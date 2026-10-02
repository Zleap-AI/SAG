"""Immutable operation snapshots, encrypted persistence and scoped client ownership."""
from __future__ import annotations

import asyncio
import copy
import math
from contextlib import asynccontextmanager
from contextvars import ContextVar

from cryptography.fernet import Fernet, InvalidToken
from sag_api.core.errors import ConfigurationError, ForbiddenError, UpstreamError
from sag_api.db.models import Setting
from sqlalchemy import select

from .config import (
    KEYLESS_API_KEY,
    EmbeddingConnection,
    Environment,
    LLMConnection,
    error,
)

operation = ContextVar("sag_chatbot_operation", default=None)
query_scope = ContextVar("sag_chatbot_query_scope", default=False)
chat_replay = ContextVar("sag_chatbot_run_replay", default=None)
stock_connection_test = ContextVar("sag_chatbot_stock_connection_test", default=False)
manager = None


def safe_failure(target, exc):
    status = getattr(exc, "status_code", None)
    if status in {401, 403} or isinstance(exc, ConfigurationError):
        return ConfigurationError(f"Chatbot {target} authentication failed; check the separate connection's credentials")
    return UpstreamError(
        f"Chatbot {target} connection failed; check endpoint, model, provider and server availability",
        retryable=status in {408, 429, 500, 502, 503, 504},
    )


class Snapshot:
    def __init__(self, environment, stock, connections):
        self.connections = connections
        self.stock = stock.model_copy(deep=True)
        self.settings, self.responses_config = environment.resolve(self.stock, connections)
        self.adapters = {}
        self.references = 0
        self.closed = False
        self.replay = {}
        self.responses_handler = None

    def adapter(self, kind):
        if kind not in self.adapters:
            from sag_api.sag.config_builder import _structured_output_mode
            from zleap.sag.config import EmbeddingConfig, LLMConfig
            from zleap.sag.core.adapters.defaults import (
                OpenAIEmbeddingAdapter,
                OpenAILLMAdapter,
            )

            if kind == "llm":
                settings = self.settings
                config = LLMConfig(
                    provider="litellm", model=settings.routed_llm_model, api_key=settings.llm_api_key or KEYLESS_API_KEY,
                    base_url=settings.llm_base_url, temperature=settings.effective_llm_temperature,
                    max_tokens=settings.llm_max_tokens, timeout=max(1, (settings.llm_timeout_ms + 999) // 1000),
                    max_retries=settings.llm_max_retries, structured_output_mode=_structured_output_mode(settings),
                )
                self.adapters[kind] = OpenAILLMAdapter(config=config)
            else:
                connection = self.connections["embedding"]
                config = EmbeddingConfig(
                    model=self.stock.embedding_model, api_key=connection.api_key or KEYLESS_API_KEY, base_url=connection.base_url,
                    schema_dimensions=self.stock.effective_embedding_schema_dimensions,
                    request_dimensions=self.stock.effective_embedding_request_dimensions,
                    timeout=self.stock.embedding_timeout,
                )
                if connection.api_key:
                    self.adapters[kind] = OpenAIEmbeddingAdapter(config=config)
                else:
                    class KeylessEmbeddingAdapter(OpenAIEmbeddingAdapter):
                        async def _get(self):
                            initializing = self._client is None
                            client = await super()._get()
                            if initializing:
                                from functools import partial

                                from openai import omit
                                # Omission must be per request; SDK defaults still enforce authentication.
                                # Wrap only this owned client's call, preserving stock retries/cleanup.
                                client.client.embeddings.create = partial(
                                    client.client.embeddings.create, extra_headers={"Authorization": omit},
                                )
                            return client
                    self.adapters[kind] = KeylessEmbeddingAdapter(config=config)
        return self.adapters[kind]

    async def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            await asyncio.gather(*(adapter.close() for adapter in self.adapters.values()))
        finally:
            self.adapters.clear()
            self.replay.clear()


class Manager:
    def __init__(self, environment: Environment, stock):
        self.environment = environment
        self.stock = stock
        self.persisted = {}
        self.lock = asyncio.Lock()
        self.loaded = False

    def cipher(self):
        key = self.environment.encryption_key
        if not key:
            raise error("Set SAG_CHATBOT_CONFIG_ENCRYPTION_KEY through deployment secrets before saving UI API keys")
        try:
            return Fernet(key.encode())
        except (ValueError, TypeError):
            raise error("SAG_CHATBOT_CONFIG_ENCRYPTION_KEY must be a valid Fernet key") from None

    def decrypt(self, encrypted):
        try:
            return self.cipher().decrypt(encrypted.encode()).decode()
        except (InvalidToken, UnicodeError, AttributeError):
            raise error("Cannot decrypt saved chatbot credentials; restore the original encryption key or clear the chatbot_config row and re-enter keys") from None

    def connections(self, persisted=None):
        persisted = self.persisted if persisted is None else persisted
        if not self.environment.locked:
            if not isinstance(persisted, dict) or set(persisted) - {"llm", "embedding"}:
                raise error("Invalid saved chatbot_config row; back up and repair or remove only this settings row")
            for target, cls in (("llm", LLMConnection), ("embedding", EmbeddingConnection)):
                entry = persisted.get(target, {})
                if not isinstance(entry, dict) or set(entry) - ((set(cls.model_fields) - {"api_key"}) | {"api_key_encrypted"}):
                    raise error("Invalid saved chatbot_config row; only encrypted UI credentials are supported")
        result = {}
        for target, cls in (("llm", LLMConnection), ("embedding", EmbeddingConnection)):
            values = dict(self.environment.connections[target])
            saved = {} if self.environment.locked else dict(persisted.get(target, {}))
            encrypted = saved.pop("api_key_encrypted", None)
            values.update(saved)
            if encrypted:
                values["api_key"] = self.decrypt(encrypted)
            elif saved:
                # An environment key must never follow a UI draft to a different host/provider.
                provisional = cls.model_construct(**values)
                env_connection = cls.model_construct(**self.environment.connections[target])
                if provisional.identity() != env_connection.identity():
                    values["api_key"] = ""
            try:
                result[target] = cls(**values)
            except ValueError:
                raise error(f"Invalid {target} connection; check endpoint/model fields") from None
        return result

    def snapshot(self, persisted=None):
        return Snapshot(self.environment, self.stock, self.connections(persisted))

    async def load(self, session_factory):
        async with self.lock:
            async with session_factory() as session:
                row = await self.row(session)
                saved = copy.deepcopy(row.value if row else {})
            self.snapshot(saved)  # Fail startup before queues start on unreadable credentials or invalid settings.
            self.persisted = saved
            self.loaded = True

    @staticmethod
    async def row(session):
        return await session.scalar(select(Setting).where(Setting.scope == "global", Setting.key == "chatbot_config"))

    def draft(self, patch, *, persist):
        if self.environment.locked:
            raise ForbiddenError("Chatbot connections are controlled by SAG_LOCK_CHATBOT_CONFIG")
        saved = copy.deepcopy(self.persisted)
        current = self.connections()
        ephemeral = {}
        for target, changes in patch.items():
            if changes is None:
                continue
            if any(value is None for value in changes.values()):
                raise error("Connection fields cannot be null; use blank API keys to retain credentials")
            changes = dict(changes)
            key = changes.pop("api_key", "")
            entry = saved.setdefault(target, {})
            entry.update(changes)
            cls = type(current[target])
            try:
                candidate = cls(**{
                    **current[target].model_dump(), **changes,
                    "api_key": key or current[target].api_key,
                })
            except ValueError:
                raise error(f"Invalid {target} draft") from None
            changed_identity = candidate.identity() != current[target].identity()
            for field in changes:
                entry[field] = getattr(candidate, field)
            if key:
                if persist:
                    entry["api_key_encrypted"] = self.cipher().encrypt(key.encode()).decode()
                else:
                    ephemeral[target] = key
            elif changed_identity:
                # Blank keys permit a keyless endpoint without forwarding the previous UI key.
                # connections() only reuses an environment key for its matching identity.
                entry.pop("api_key_encrypted", None)
        if persist:
            return saved, self.snapshot(saved)
        # Test keys live only in the in-memory draft, never in persistence.
        connections = self.connections_for_test(saved, ephemeral)
        return saved, Snapshot(self.environment, self.stock, connections)

    def connections_for_test(self, saved, keys):
        # Use the same merge/credential checks as persistence without requiring Fernet for unsaved keys.
        result = {}
        for target, cls in (("llm", LLMConnection), ("embedding", EmbeddingConnection)):
            values = dict(self.environment.connections[target])
            entry = dict(saved.get(target, {}))
            encrypted = entry.pop("api_key_encrypted", None)
            values.update(entry)
            if target in keys:
                values["api_key"] = keys[target]
            elif encrypted:
                values["api_key"] = self.decrypt(encrypted)
            elif cls.model_construct(**values).identity() != cls.model_construct(**self.environment.connections[target]).identity():
                values["api_key"] = ""
            try:
                result[target] = cls(**values)
            except ValueError:
                raise error(f"Invalid {target} test connection") from None
        return result

    async def save(self, session, patch):
        async with self.lock:
            saved, snapshot = self.draft(patch, persist=True)
            row = await self.row(session)
            if row is None:
                row = Setting(scope="global", key="chatbot_config", value=saved)
                session.add(row)
            else:
                row.value = saved
            await session.commit()
            self.persisted = saved  # No await between commit and publication.
            return self.public(snapshot)

    def public(self, snapshot=None):
        snapshot = snapshot or self.snapshot()
        result = {"locked": self.environment.locked, "encryption_configured": bool(self.environment.encryption_key)}
        for target, connection in snapshot.connections.items():
            values = connection.model_dump(exclude={"api_key"})
            values["api_key_set"] = bool(connection.api_key)
            saved = {} if self.environment.locked else self.persisted.get(target, {})
            values["sources"] = {
                field: "ui" if field in saved else "environment" if f"SAG_CHATBOT_{target.upper()}_{field.upper()}" in self.environment.env else "default"
                for field in values if field not in {"sources", "api_key_set"}
            }
            values["credential_source"] = "ui" if saved.get("api_key_encrypted") else "environment" if connection.api_key else "unset"
            result[target] = values
        active = snapshot.settings
        from .config import TUNING
        result["llm"]["effective"] = {
            field: getattr(active, "llm_" + field) for field in TUNING
        }
        # Request options may contain administrative secrets: expose presence and source, never raw values.
        result["llm"]["effective"]["extra_body"] = bool(active.llm_extra_body)
        result["llm"]["effective"]["temperature"] = active.effective_llm_temperature
        result["llm"]["tuning_sources"] = {
            field: "environment" if "llm_" + field in self.environment.tuning and snapshot.connections["llm"].enabled else "inherited"
            for field in TUNING
        }
        if snapshot.connections["llm"].enabled:
            from sag_api.core.model_providers import get_model_provider

            from .config import PROVIDERS
            spec = PROVIDERS[active.llm_provider]
            protocol = "openai_responses" if snapshot.responses_config else spec.protocol
            if "llm_extra_body" not in self.environment.tuning and protocol != get_model_provider(snapshot.stock.llm_provider).protocol:
                result["llm"]["tuning_sources"]["extra_body"] = "protocol_default"
            if not spec.temperature_configurable:
                result["llm"]["tuning_sources"]["temperature"] = "provider"
        result["embedding"].update(
            model=snapshot.stock.embedding_model,
            schema_dimensions=snapshot.stock.effective_embedding_schema_dimensions,
            request_dimensions=snapshot.stock.effective_embedding_request_dimensions,
        )
        return result

    @asynccontextmanager
    async def scope(self, *, query=False, snapshot=None):
        active = snapshot or operation.get() or self.snapshot()
        if active.closed:
            active = self.snapshot()
        active.references += 1
        token = operation.set(active)
        query_token = query_scope.set(query or query_scope.get())
        try:
            yield active
        finally:
            query_scope.reset(query_token)
            operation.reset(token)
            active.references -= 1
            if active.references == 0:
                await active.close()


class ScopedAdapter:
    """Registry wrapper: durable calls delegate to the original, query calls to their snapshot."""
    def __init__(self, original, kind):
        self.original, self.kind = original, kind

    def __getattr__(self, name):
        return getattr(self.original, name)

    async def invoke(self, name, *args, **kwargs):
        active = operation.get()
        if not query_scope.get() or active is None or not active.connections[self.kind].enabled:
            return await getattr(self.original, name)(*args, **kwargs)
        try:
            result = await getattr(active.adapter(self.kind), name)(*args, **kwargs)
            if self.kind == "embedding":
                vectors = [result] if name == "generate" else result
                expected = active.stock.effective_embedding_schema_dimensions
                count = 1 if name == "generate" else len(args[0])
                if len(vectors) != count or any(
                    len(vector) != expected or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in vector)
                    for vector in vectors
                ):
                    raise error("Query embedding returned malformed vectors or mismatched dimensions")
            return result
        except ConfigurationError:
            raise
        except Exception as exc:  # noqa: BLE001 -- hide provider response bodies at the query boundary.
            raise safe_failure(self.kind, exc) from None

    async def chat(self, *args, **kwargs):
        return await self.invoke("chat", *args, **kwargs)

    async def chat_with_schema(self, *args, **kwargs):
        return await self.invoke("chat_with_schema", *args, **kwargs)

    async def chat_with_schema_once(self, *args, **kwargs):
        return await self.invoke("chat_with_schema_once", *args, **kwargs)

    async def generate(self, *args, **kwargs):
        return await self.invoke("generate", *args, **kwargs)

    async def batch_generate(self, *args, **kwargs):
        return await self.invoke("batch_generate", *args, **kwargs)

    async def close(self):
        await self.original.close()


class SettingsView:
    """Read-only policy view; it never changes the stock settings singleton."""
    def __init__(self, stock, *, context_only=False):
        self.stock, self.context_only = stock, context_only

    def __getattr__(self, name):
        active = operation.get()
        if active is not None and (query_scope.get() or (self.context_only and name == "llm_context_window")):
            return getattr(active.settings, name)
        return getattr(self.stock, name)


class CaptureMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not manager.loaded:
            return await self.app(scope, receive, send)
        token = stock_connection_test.set(scope.get("path") == "/api/v1/system/model-config/test")
        try:
            async with manager.scope():
                await self.app(scope, receive, send)
        finally:
            stock_connection_test.reset(token)
