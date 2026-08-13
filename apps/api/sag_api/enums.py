"""跨层共享的枚举（模型 / schema / 服务均可导入，无副作用）。"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

SearchStrategy = Literal["vector", "multi", "multi_es_fast"]
SEARCH_STRATEGIES = frozenset({"vector", "multi", "multi_es_fast"})

# 策略对底层引擎能力的依赖。缺失能力的部署会在 UI 灰置该选项；后端调用则拒绝。
SEARCH_STRATEGY_REQUIREMENTS: dict[str, frozenset[str]] = {
    "vector": frozenset(),
    "multi": frozenset(),
    "multi_es_fast": frozenset({"lexical_search"}),
}


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
    OCTX_PREFLIGHT = "octx_preflight"
    OCTX_IMPORT = "octx_import"
    OCTX_EXPORT = "octx_export"
    OCTX_GC_INSTALLATION = "octx_gc_installation"
    OCTX_GC_TRANSFER = "octx_gc_transfer"


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


class OctxAssetOwnership(StrEnum):
    LOCAL = "local"
    IMPORTED = "imported"


class OctxReleaseOrigin(StrEnum):
    IMPORT = "import"
    EXPORT = "export"


class OctxInstallationStatus(StrEnum):
    SHADOW = "shadow"
    ACTIVE = "active"
    RETAINED = "retained"
    GC = "gc"
    FAILED = "failed"


class OctxTransferDirection(StrEnum):
    IMPORT = "import"
    EXPORT = "export"


class OctxImportAction(StrEnum):
    UPDATE = "update"
    NEW = "new"
    CANCEL = "cancel"


class OctxExportAction(StrEnum):
    EXPORT_READY_ONLY = "export_ready_only"
    CANCEL = "cancel"


class OctxTransferStatus(StrEnum):
    UPLOADED = "uploaded"
    VALIDATING = "validating"
    DECISION_REQUIRED = "decision_required"
    QUEUED = "queued"
    IMPORTING = "importing"
    INDEXING = "indexing"
    SWITCHING = "switching"
    EXPORTING = "exporting"
    PACKAGING = "packaging"
    READY = "ready"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
