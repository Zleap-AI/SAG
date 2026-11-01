"""SAG2 retrieval over native, published document inputs only."""

from __future__ import annotations

import asyncio

from sqlalchemy import select

from sag_api.core.errors import ConfigurationError
from sag_api.sag.sag2_engine import Sag2EngineHolder, jev_connection_issue
from sag_api.sag.sag2_source_engine import _BINDINGS, Sag2SourceEngine, native_config, native_namespace


class Sag2NativeSearchHolder(Sag2EngineHolder):
    async def _start(self, settings):
        issue = jev_connection_issue(settings)
        if issue:
            code, message = issue
            raise ConfigurationError(message, code=code)
        scorer = self._scorer_factory(native_config(settings)) if self._scorer_factory else None
        adapter = Sag2SourceEngine(settings, "search", scorer=scorer)
        await adapter.start()
        return adapter.native

    async def _count_scope(self, engine, data_source_ids):
        # source_state counts indexed chunks without loading text/token streams.
        gate = asyncio.Semaphore(16)

        async with engine.store.engine.connect() as connection:
            pairs = (
                await connection.execute(
                    select(_BINDINGS.c.namespace, _BINDINGS.c.input_id).where(
                        _BINDINGS.c.namespace.in_(data_source_ids),
                    )
                )
            ).all()

        async def count(namespace, input_id):
            async with gate:
                state = await engine.source_state(input_id, data_source_id=namespace)
                return int(state.chunk_count) if state is not None else 0

        return sum(await asyncio.gather(*(count(namespace, input_id) for namespace, input_id in pairs)))

    async def search(self, targets, query, *, top_k, requested_sources, source_limit_applied, source_ids_by_config):
        if source_ids_by_config is None:
            raise ConfigurationError("SAG2 检索必须限定已发布文档范围")
        mapping = {
            native_namespace(config_id, input_id): config_id
            for config_id, _ in targets
            for input_id in source_ids_by_config.get(config_id, ())
        }
        scopes = {
            native_namespace(config_id, input_id): (input_id,)
            for config_id, _ in targets
            for input_id in source_ids_by_config.get(config_id, ())
        }
        outcome = await super().search(
            [(namespace, None) for namespace in mapping],
            query,
            top_k=top_k,
            requested_sources=requested_sources,
            source_limit_applied=source_limit_applied,
            source_ids_by_config=scopes,
        )
        for section in outcome.sections:
            section.source_config_id = mapping[section.source_config_id]
            section.metadata["engine_kind"] = "sag2"
        outcome.stats["sources"] = len(targets)
        return outcome
