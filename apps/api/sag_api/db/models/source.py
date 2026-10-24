from __future__ import annotations

from sqlalchemy import JSON, Integer, String, Text
from sqlalchemy import Enum as SAEnum
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.orm import Mapped, mapped_column

from sag_api.db.base import Base, IDMixin, TimestampMixin
from sag_api.enums import ConnectorKind, SourceStatus, SourceType


class Source(IDMixin, TimestampMixin, Base):
    """信源 —— 与 zleap-sag 的一个数据源（source_config_id）一一对应。"""

    __tablename__ = "sources"

    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    source_type: Mapped[SourceType] = mapped_column(
        SAEnum(SourceType, native_enum=False, length=16), default=SourceType.DOCUMENT
    )
    connector_kind: Mapped[ConnectorKind] = mapped_column(
        SAEnum(ConnectorKind, native_enum=False, length=32),
        default=ConnectorKind.FILE_UPLOAD,
    )
    # zleap-sag 数据源标识（一实例一源）
    sag_source_config_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    # 连接器配置 + 引擎覆盖（如 per-source language / entity_types）
    config: Mapped[dict] = mapped_column("config_json", JSON, default=dict)
    status: Mapped[SourceStatus] = mapped_column(
        SAEnum(SourceStatus, native_enum=False, length=16), default=SourceStatus.ACTIVE
    )
    document_count: Mapped[int] = mapped_column(Integer, default=0)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    event_count: Mapped[int] = mapped_column(Integer, default=0)
    # 该信源的默认检索模式；None 表示跟随全局默认。搜索与问答在范围内信源一致时采用，
    # 不改变文档入库流程。
    default_search_strategy: Mapped[str | None] = mapped_column(String(32), nullable=True)

    @hybrid_property
    def is_deleting(self) -> bool:
        return self.status in {SourceStatus.DELETING, SourceStatus.DELETE_FAILED}

    @is_deleting.inplace.expression
    @classmethod
    def _is_deleting_expression(cls):
        return cls.status.in_([SourceStatus.DELETING, SourceStatus.DELETE_FAILED])
