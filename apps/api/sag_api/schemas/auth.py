from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator


class RegisterRequest(BaseModel):
    email: str
    password: str = Field(min_length=8, max_length=128)
    name: str = ""

    @field_validator("email")
    @classmethod
    def _email(cls, v: str) -> str:
        v = v.strip().lower()
        if "@" not in v or "." not in v.split("@")[-1]:
            raise ValueError("邮箱格式不正确")
        return v


class LoginRequest(BaseModel):
    name: str = Field(default="", max_length=120)
    email: str = Field(default="", max_length=255)
    password: str | None = Field(default=None, max_length=128)

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("请先填写名字")
        return v

    @field_validator("email")
    @classmethod
    def _optional_email(cls, v: str) -> str:
        v = v.strip().lower()
        if v and ("@" not in v or "." not in v.split("@")[-1]):
            raise ValueError("邮箱格式不正确")
        return v


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    email: str
    name: str
    created_at: datetime


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserOut
