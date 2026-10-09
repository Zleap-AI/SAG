"""Owned zleap-sag engine lifecycle with a bounded read-engine LRU."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Protocol


class Engine(Protocol):
    async def start(self) -> None: ...
    async def aclose(self) -> None: ...
    async def health(self) -> Any: ...


EngineFactory = Callable[[object, str | None], Engine]


def _default_factory(config: object, namespace: str | None) -> Engine:
    from zleap.sag import DataEngine

    return DataEngine(config, data_source_id=namespace)  # type: ignore[arg-type]


@dataclass(slots=True)
class _ReadEntry:
    engine: Engine
    active: int = 0


class EnginePool:
    """Own one search engine and at most `max_read_engines` namespace engines."""

    def __init__(
        self,
        config: object,
        namespaces: set[str] | frozenset[str],
        *,
        max_read_engines: int,
        factory: EngineFactory = _default_factory,
    ) -> None:
        if max_read_engines < 1:
            raise ValueError("max_read_engines must be positive")
        self._config = config
        self._namespaces = frozenset(namespaces)
        self._max_read_engines = max_read_engines
        self._factory = factory
        self._condition = asyncio.Condition()
        self._search: Engine | None = None
        self._reads: OrderedDict[str, _ReadEntry] = OrderedDict()
        self._closed = False

    async def start(self) -> None:
        async with self._condition:
            if self._closed:
                raise RuntimeError("engine pool is closed")
            if self._search is not None:
                return
            engine = self._factory(self._config, None)
            try:
                await engine.start()
            except BaseException:
                await engine.aclose()
                raise
            self._search = engine

    async def search_engine(self) -> Engine:
        async with self._condition:
            if self._closed:
                raise RuntimeError("engine pool is closed")
            if self._search is None:
                raise RuntimeError("engine pool is not started")
            return self._search

    @asynccontextmanager
    async def read_engine(self, namespace_id: str) -> AsyncIterator[Engine]:
        if namespace_id not in self._namespaces:
            raise ValueError("namespace is not configured")
        entry: _ReadEntry
        async with self._condition:
            while True:
                if self._closed:
                    raise RuntimeError("engine pool is closed")
                existing = self._reads.get(namespace_id)
                if existing is not None:
                    entry = existing
                    entry.active += 1
                    self._reads.move_to_end(namespace_id)
                    break
                if len(self._reads) >= self._max_read_engines:
                    idle_namespace = next((name for name, candidate in self._reads.items() if candidate.active == 0), None)
                    if idle_namespace is None:
                        await self._condition.wait()
                        continue
                    idle = self._reads.pop(idle_namespace)
                    await idle.engine.aclose()
                engine = self._factory(self._config, namespace_id)
                try:
                    await engine.start()
                except BaseException:
                    await engine.aclose()
                    raise
                entry = _ReadEntry(engine=engine, active=1)
                self._reads[namespace_id] = entry
                break
        try:
            yield entry.engine
        finally:
            async with self._condition:
                entry.active -= 1
                self._condition.notify_all()

    async def health(self) -> Any:
        return await (await self.search_engine()).health()

    async def capabilities(self) -> Any:
        engine = await self.search_engine()
        return engine.capabilities()  # type: ignore[attr-defined]

    async def aclose(self) -> None:
        async with self._condition:
            if self._closed:
                return
            self._closed = True
            self._condition.notify_all()
            while any(entry.active for entry in self._reads.values()):
                await self._condition.wait()
            engines = [entry.engine for entry in self._reads.values()]
            if self._search is not None:
                engines.insert(0, self._search)
            self._reads.clear()
            self._search = None
        results = await asyncio.gather(*(engine.aclose() for engine in engines), return_exceptions=True)
        failures = [result for result in results if isinstance(result, BaseException)]
        if failures:
            raise ExceptionGroup("failed to close SAG engines", failures)
