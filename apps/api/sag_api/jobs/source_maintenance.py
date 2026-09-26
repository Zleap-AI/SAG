"""信源维护窗口的状态集合 —— 从 `InProcessAsyncQueue` 抽出的纯状态容器。

维护窗口是「同一信源上的破坏性操作（删除/重跑）必须与文档处理互斥」的实现：
某些信源存在待执行的维护任务时，该信源上的 `PROCESS_DOCUMENT` 会主动让行，
由协调器协程开启一个引擎级维护窗口，逐个派发维护任务，全部结束后再唤醒让行的任务。

本模块只承载这组状态的**存取语义**，不包含协调流程（协程调度、数据库事务、
引擎调用仍在队列侧），原因是那些流程与队列的 `_queue` / `_session_factory` /
`_engine_manager` 深度交织，搬迁会改变生命周期时序。

字段语义：

* `jobs`        —— 每个信源已登记的维护任务 id 集合（非空即「需要维护窗口」）
* `tasks`       —— 每个信源在跑的协调器协程
* `ready`       —— 维护窗口已开启、可派发任务的信源
* `dispatched`  —— 每个信源当前已派发出去的维护任务 id
* `closing`     —— 正在关闭维护窗口的信源（关闭期间拒绝新派发）
* `stop_requested` —— 被请求尽快让路的信源（同步删除用）
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

__all__ = ["SourceMaintenanceState"]


@dataclass
class SourceMaintenanceState:
    """信源维护窗口的全部内存状态。

    所有键都是 `source_id`，与队列的 worker / 事务边界无关，
    因此可以整体清空（停机）或按源增删而无需触碰队列其它状态。
    """

    jobs: dict[str, set[str]] = field(default_factory=dict)
    tasks: dict[str, asyncio.Task] = field(default_factory=dict)
    ready: set[str] = field(default_factory=set)
    dispatched: dict[str, str] = field(default_factory=dict)
    closing: set[str] = field(default_factory=set)
    stop_requested: set[str] = field(default_factory=set)
