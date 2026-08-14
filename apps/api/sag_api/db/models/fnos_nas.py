from __future__ import annotations

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from sag_api.db.base import Base, IDMixin, TimestampMixin


class FnOSNasLegacyFolder(IDMixin, TimestampMixin, Base):
    """A manually authorized legacy fnOS folder in this private worker database."""

    __tablename__ = "fnos_nas_legacy_folders"

    path: Mapped[str] = mapped_column(String(2048), unique=True)
    display_label: Mapped[str] = mapped_column(String(2048))
