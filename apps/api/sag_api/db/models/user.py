from __future__ import annotations

from sqlalchemy import Boolean, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from sag_api.db.base import Base, IDMixin, TimestampMixin


class User(IDMixin, TimestampMixin, Base):
    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    # 旧版名字登录会写入隐式默认哈希；迁移后必须先用私有 bootstrap 凭据设定真实密码。
    password_initialized: Mapped[bool] = mapped_column(Boolean, default=False)
    auth_version: Mapped[int] = mapped_column(Integer, default=0)
    # password 模式固定为 1，唯一索引把并发首建收敛为单一 owner；dev 用户保持 NULL。
    auth_singleton: Mapped[int | None] = mapped_column(Integer, nullable=True)
    name: Mapped[str] = mapped_column(String(120), default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
