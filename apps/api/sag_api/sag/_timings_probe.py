"""引擎检索耗时的采集桶（ContextVar）。

历史上这里还会 monkey-patch zleap 检索链的私有方法，把每步 `_timings` 复制进
ContextVar。该补丁的目标（`MultiSearcherES.search_for_sections` /
`VectorSearcher.search_chunks_for_rerank` / `modules.search.multi_vector`）在
zleap-sag 0.12.0 与 0.13.0 上**都不存在**——导入即 ImportError，被静默跳过，
`_installed` 永不置位，因此它从未生效过。0.13.0 升级时删除，避免继续误导。

保留的 `capture_scope` / `release_scope` 是活代码：`engine_manager` 用它把
`engine_timings` 键写进 `SearchOutcome.stats`。

注意该键在本探针失效的前提下**不会出现**（而非等于空 dict）：写入点由
`if bucket:` 守卫，而桶里从未有人写过值。所以调用方读不到 `engine_timings`
是既有行为，本次升级不做改变。要让耗时真正有值，应改用 zleap 公开的
`SearchOptions(include_stage_stats=True)`（产出 `stats["stages"]`），属单独事项。
"""

from __future__ import annotations

import contextvars

_engine_timings_var: contextvars.ContextVar[dict[str, float] | None] = contextvars.ContextVar(
    "engine_timings_bucket",
    default=None,
)


def capture_scope() -> tuple[dict[str, float], contextvars.Token[dict[str, float] | None]]:
    """开一个 timings 桶;在 `_search_raw` 内 set,离开时 reset。"""
    bucket: dict[str, float] = {}
    token = _engine_timings_var.set(bucket)
    return bucket, token


def release_scope(token: contextvars.Token[dict[str, float] | None]) -> None:
    _engine_timings_var.reset(token)


__all__ = [
    "capture_scope",
    "release_scope",
]
