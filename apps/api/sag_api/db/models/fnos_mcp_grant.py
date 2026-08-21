from __future__ import annotations

from datetime import datetime

from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from sag_api.db.base import Base, IDMixin, TimestampMixin, UTCDateTime


class FnOSMcpGrant(IDMixin, TimestampMixin, Base):
    """A revocable external-MCP credential for one fnOS workspace user.

    The random secret is never stored.  The gateway verifies its signed
    routing envelope, while the worker checks this persisted digest so a
    revoked credential stops working immediately.
    """

    __tablename__ = "fnos_mcp_grants"

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    secret_digest: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), index=True)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
