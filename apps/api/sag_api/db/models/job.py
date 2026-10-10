from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Float, ForeignKey, Integer, Text, cast
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql.elements import ColumnElement

from sag_api.db.base import Base, IDMixin, TimestampMixin, UTCDateTime
from sag_api.enums import JobStatus, JobType


class Job(IDMixin, TimestampMixin, Base):
    __tablename__ = "jobs"

    type: Mapped[JobType] = mapped_column(SAEnum(JobType, native_enum=False, length=32))
    status: Mapped[JobStatus] = mapped_column(
        SAEnum(JobStatus, native_enum=False, length=16), default=JobStatus.QUEUED, index=True
    )
    source_id: Mapped[str | None] = mapped_column(
        ForeignKey("sources.id", ondelete="CASCADE"), nullable=True, index=True
    )
    document_id: Mapped[str | None] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), nullable=True, index=True
    )
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    payload: Mapped[dict] = mapped_column("payload_json", JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)


def job_payload_matches(session: AsyncSession, expected: dict) -> ColumnElement[bool]:
    """Compare a saved payload for CAS without changing the JSON storage column."""
    if session.get_bind().dialect.name == "postgresql":
        # PostgreSQL JSON has no equality operator; JSONB compares its values.
        return cast(Job.payload, JSONB) == expected
    return Job.payload == expected
