class JobPaused(Exception):
    """协作式暂停信号；不是失败，不触发重试。"""


class JobDeleted(Exception):
    """删除处理器已删除自身 Job，队列不应再写入该行。"""


class JobYielded(Exception):
    """任务因内部调度临时让行；不是用户暂停，也不是失败。"""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason
