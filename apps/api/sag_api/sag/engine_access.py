"""引擎访问接缝 —— 读侧协作者访问 `EngineManager` 能力的唯一入口。

`UniverseReader` / `ContentReader` / `SearchReader` 不直接持有 `EngineManager`，
只持有这个接缝；接缝本身持有管理器并**惰性**转发属性查找。

为什么惰性而非构造时绑定：既有测试与调用方用
``monkeypatch.setattr(manager, "use", fake)`` / ``manager._search_raw`` 等方式
在实例上打桩。若在构造时把方法对象绑定进接缝，打桩就再也影响不到读侧代码
（属性查找发生在构造那一刻）。用属性动态取值可保持原有打桩语义不变，
同时读侧依旧只依赖接缝，不出现 `manager -> reader -> manager` 的回环依赖。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sag_api.core.config import Settings
    from sag_api.sag.engine_manager import EngineManager


class EngineAccess:
    """把 `EngineManager` 的槽 / 会话 / 策略 / 生命周期能力转发给读侧协作者。"""

    def __init__(self, manager: EngineManager) -> None:
        self._manager = manager

    # --- 引擎槽与运行时 ---

    async def slot(self, source_config_id: str, source: Any = None) -> Any:
        return await self._manager._slot(source_config_id, source)

    async def relational_session_factory(self, source_config_id: str, source: Any = None) -> Any:
        return await self._manager._relational_session_factory(source_config_id, source)

    async def ensure_read_runtime(self, sources_by_config: dict[str, Any]) -> None:
        await self._manager._ensure_read_runtime(sources_by_config)

    def use(self, source_config_id: str, source: Any = None) -> Any:
        return self._manager.use(source_config_id, source)

    # --- 配置 ---

    @property
    def settings(self) -> Settings:
        return self._manager._settings

    # --- 检索策略（规则仍由 EngineManager 持有）---

    def effective_search_strategy(self, requested: str | None) -> str:
        return self._manager._effective_search_strategy(requested)

    def zleap_engine_strategy(self, facade_strategy: str) -> str:
        return self._manager._zleap_engine_strategy(facade_strategy)

    # --- 读侧互相调用（经管理器转发，保持打桩语义）---

    async def search(self, *args: Any, **kwargs: Any) -> Any:
        return await self._manager.search(*args, **kwargs)

    async def search_many(self, *args: Any, **kwargs: Any) -> Any:
        return await self._manager.search_many(*args, **kwargs)

    async def search_raw(self, *args: Any, **kwargs: Any) -> Any:
        return await self._manager._search_raw(*args, **kwargs)

    async def search_chunk_vectors(self, *args: Any, **kwargs: Any) -> Any:
        return await self._manager._search_chunk_vectors(*args, **kwargs)

    async def universe_entity_event_counts(self, *args: Any, **kwargs: Any) -> Any:
        return await self._manager._universe_entity_event_counts(*args, **kwargs)

    async def universe_event_bundles(self, *args: Any, **kwargs: Any) -> Any:
        return await self._manager._universe_event_bundles(*args, **kwargs)
