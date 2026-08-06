from __future__ import annotations

from sqlalchemy import BigInteger, ForeignKey, Index, Integer, String, Text
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column

from sag_api.db.base import Base, IDMixin, TimestampMixin
from sag_api.enums import DocumentStatus


class Document(IDMixin, TimestampMixin, Base):
    __tablename__ = "documents"
    __table_args__ = (
        Index("ix_documents_source_sag_source", "source_id", "sag_source_id"),
    )

    source_id: Mapped[str] = mapped_column(
        ForeignKey("sources.id", ondelete="CASCADE"), index=True
    )
    filename: Mapped[str] = mapped_column(String(512))
    content_type: Mapped[str] = mapped_column(String(128), default="application/octet-stream")
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    storage_path: Mapped[str] = mapped_column(String(1024))
    status: Mapped[DocumentStatus] = mapped_column(
        SAEnum(DocumentStatus, native_enum=False, length=16), default=DocumentStatus.PENDING
    )
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    event_count: Mapped[int] = mapped_column(Integer, default=0)
    progress: Mapped[int] = mapped_column(Integer, default=0)
    token_usage: Mapped[int] = mapped_column(BigInteger, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 失败归属：责任层（api/engine/llm/store）与链路环节（parse/chunk/extract/...），
    # 便于研发从导出日志直接定位。仅在 status=failed 时有值。
    error_layer: Mapped[str | None] = mapped_column(String(16), nullable=True)
    error_stage: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # zleap-sag ingest 返回的 source_id（用于溯源）
    sag_source_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
