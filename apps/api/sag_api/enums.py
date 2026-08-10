"""跨层共享的枚举（模型 / schema / 服务均可导入，无副作用）。"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

SearchStrategy = Literal["vector", "multi"]
SEARCH_STRATEGIES = frozenset({"vector", "multi"})


def normalize_search_strategy(value: str) -> str:
    """把已下线的原子检索迁移到精确检索；其他值交给调用方校验。"""
    return "multi" if value == "atomic" else value


class SourceType(StrEnum):
    DOCUMENT = "document"
    WEB = "web"
    MESSAGE = "message"
    AUDIO = "audio"


class ConnectorKind(StrEnum):
    FILE_UPLOAD = "file_upload"
    WEB = "web"
    # 预留：NOTION = "notion"; S3 = "s3"; CONFLUENCE = "confluence"; ...


# 连接器 → 默认信源类型
CONNECTOR_SOURCE_TYPE = {
    ConnectorKind.FILE_UPLOAD: SourceType.DOCUMENT,
    ConnectorKind.WEB: SourceType.WEB,
}


class SourceStatus(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    ERROR = "error"


class DocumentStatus(StrEnum):
    PENDING = "pending"        # 已登记，待处理
    LOADING = "loading"        # ingest 中（解析 → 分块 → 入库 → 向量）
    EXTRACTING = "extracting"  # extract 中（事件 / 实体抽取）
    PAUSING = "pausing"        # 已收到停止命令，等待在途分块完成
    PAUSED = "paused"          # 抽取已暂停，可从 chunk 断点继续
    DELETING = "deleting"      # 已收到删除命令，后台正在停止并清理
    DELETE_FAILED = "delete_failed"
    READY = "ready"            # 处理完成，可检索
    FAILED = "failed"


class JobType(StrEnum):
    PROCESS_DOCUMENT = "process_document"
    REPROCESS_DOCUMENT = "reprocess_document"
    DELETE_DOCUMENT = "delete_document"
    SYNC_SOURCE = "sync_source"
    INDEX_UNIVERSE = "index_universe"


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    PAUSED = "paused"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class MessageRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"


class MessageStatus(StrEnum):
    OK = "ok"
    FAILED = "failed"
    CANCELLED = "cancelled"


class BindingTargetType(StrEnum):
    SOURCE = "source"
    MCP_SERVER = "mcp_server"  # Phase C：挂载 MCP server 作为工具来源
