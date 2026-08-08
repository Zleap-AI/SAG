class JobPaused(Exception):
    """协作式暂停信号；不是失败，不触发重试。"""


class JobDeleted(Exception):
    """删除处理器已删除自身 Job，队列不应再写入该行。"""
