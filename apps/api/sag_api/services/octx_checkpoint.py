"""OCTX transfer checkpoint 的读写收敛。

`OctxTransfer.checkpoint` 是 JSON 列（`mapped_column("checkpoint_json", JSON, default=dict)`）。
历史上调用方各处手写 `transfer.checkpoint = {**dict(transfer.checkpoint or {}), ...}`，
同一模式重复十余次；本模块把它收敛为显式操作，避免改 schema 时漏改。

约定（有意保持与旧代码逐字等价）：

* `checkpoint_of` 总是返回**同一个**可变 dict（缺省时为 transfer 上新建的空 dict），
  就地修改后由调用方赋值回去。
* `merge_checkpoint` 保留旧键、覆盖新键，语义等同 `{**old, **new}`。
* 整体替换（决策路径里先清空再重建）仍由调用方显式赋值，不走这里 ——
  那种写法的“覆盖而非合并”语义不能丢。
"""

from __future__ import annotations

from typing import Any, Protocol

__all__ = ["merge_checkpoint"]


class CheckpointHost(Protocol):
    """最小结构约定：任何带可变 `checkpoint` dict 的持久化对象。"""

    checkpoint: dict[str, Any]


def checkpoint_of(transfer: CheckpointHost) -> dict[str, Any]:
    """取当前 checkpoint（永不返回 None）。

    若列上是 None，就地写入空 dict 并返回，保证调用方拿到的是可写对象。
    """
    current = transfer.checkpoint
    if current is None:
        current = {}
        transfer.checkpoint = current
    return current


def merge_checkpoint(transfer: CheckpointHost, **updates: Any) -> dict[str, Any]:
    """把 updates 合并进现有 checkpoint 并写回，返回合并结果。

    等价于 `transfer.checkpoint = {**dict(transfer.checkpoint or {}), **updates}`。
    """
    merged = {**checkpoint_of(transfer), **updates}
    transfer.checkpoint = merged
    return merged
