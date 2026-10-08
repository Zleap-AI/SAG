"""Unpublished successful chunk outcomes, retained until document reprocessing/deletion."""

from sqlalchemy import JSON, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from sag_api.db.base import Base


class DocumentExtractionCheckpoint(Base):
    __tablename__ = "document_extraction_checkpoints"

    extraction_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    chunk_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), index=True)
    fingerprint: Mapped[str] = mapped_column(String(64))
    outcome: Mapped[dict] = mapped_column(JSON)
