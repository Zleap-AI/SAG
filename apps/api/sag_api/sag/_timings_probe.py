"""捕获 zleap 检索链底层的 `_timings` 字典,不动 zleap 源码。

zleap 的 `searcher.search()` 会调用 `MultiSearcherES.search_for_rerank` /
`VectorSearcher.search_chunks_for_rerank`,这两处底层返回 dict 里带 `_timings`
—— 每一个 step 的耗时都在里面。但外层 `searcher.search()` 只保留 sections 和
自己算的 `stats.timing.total`,把内部 `_timings` 丢了。

这里通过一次性替换两个类方法,把 `_timings` 复制到 ContextVar,让上层
`_search_raw` 能读到、拼进 `SearchOutcome.stats["engine_timings"]`。
"""

from __future__ import annotations

import contextvars
from typing import Any

_engine_timings_var: contextvars.ContextVar[dict[str, float] | None] = contextvars.ContextVar(
    "engine_timings_bucket",
    default=None,
)

_installed = False


def capture_scope() -> tuple[dict[str, float], contextvars.Token[dict[str, float] | None]]:
    """开一个 timings 桶;在 `_search_raw` 内 set,离开时 reset。"""
    bucket: dict[str, float] = {}
    token = _engine_timings_var.set(bucket)
    return bucket, token


def release_scope(token: contextvars.Token[dict[str, float] | None]) -> None:
    _engine_timings_var.reset(token)


def _record(prefix: str, result: Any) -> None:
    bucket = _engine_timings_var.get()
    if bucket is None or not isinstance(result, dict):
        return
    timings = result.get("_timings") or {}
    if not isinstance(timings, dict):
        return
    for key, value in timings.items():
        try:
            bucket[f"{prefix}.{key}"] = float(value)
        except (TypeError, ValueError):
            continue


def install_engine_timings_probe() -> None:
    """一次性替换 zleap 底层 search 方法,把 `_timings` 复制到 ContextVar。"""
    global _installed
    if _installed:
        return
    try:
        from zleap.sag.modules.search import multi_vector as _mv
        from zleap.sag.modules.search import vector as _vec
    except ImportError:
        # zleap 布局变了就静默跳过;上层只是多不到 engine_timings。
        return

    orig_mv = _mv.MultiSearcherES.search_for_sections

    async def wrapped_mv(self: Any, *args: Any, **kwargs: Any) -> Any:
        result = await orig_mv(self, *args, **kwargs)
        _record("multi_es", result)
        return result

    _mv.MultiSearcherES.search_for_sections = wrapped_mv
    # zleap 里 `search_for_rerank = search_for_sections`(类定义期赋值),需要显式同步。
    _mv.MultiSearcherES.search_for_rerank = wrapped_mv

    orig_vec = _vec.VectorSearcher.search_chunks_for_rerank

    async def wrapped_vec(self: Any, *args: Any, **kwargs: Any) -> Any:
        result = await orig_vec(self, *args, **kwargs)
        _record("vector", result)
        return result

    _vec.VectorSearcher.search_chunks_for_rerank = wrapped_vec

    _installed = True


__all__ = [
    "capture_scope",
    "install_engine_timings_probe",
    "release_scope",
]
