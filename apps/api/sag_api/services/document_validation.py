"""Shared document file validation for local upload and NAS discovery."""

from __future__ import annotations

from enum import StrEnum
from typing import Protocol

from sag_api.core.errors import ValidationError


class DocumentValidationSettings(Protocol):
    allowed_upload_exts: set[str]
    max_upload_mb: int


class DocumentFileRejection(StrEnum):
    EMPTY = "empty"
    UNSUPPORTED = "unsupported"
    TOO_LARGE = "too_large"


class DocumentFileValidationError(ValidationError):
    def __init__(self, message: str, *, reason: DocumentFileRejection) -> None:
        self.reason = reason
        super().__init__(message)


def validate_document_file(
    filename: str | None,
    size_bytes: int,
    settings: DocumentValidationSettings,
) -> None:
    """Preserve the existing upload validation order and user-facing messages."""
    allowed = settings.allowed_upload_exts
    if allowed:
        name = (filename or "").lower()
        extension = "." + name.rsplit(".", 1)[1] if "." in name else ""
        if extension not in allowed:
            pretty = "、".join(sorted(item.lstrip(".") for item in allowed))
            raise DocumentFileValidationError(
                f"不支持的文件类型。可上传：{pretty}",
                reason=DocumentFileRejection.UNSUPPORTED,
            )
    if size_bytes <= 0:
        raise DocumentFileValidationError("文件内容为空", reason=DocumentFileRejection.EMPTY)
    if size_bytes > settings.max_upload_mb * 1024 * 1024:
        raise DocumentFileValidationError(
            f"文件超过 {settings.max_upload_mb}MB 上限",
            reason=DocumentFileRejection.TOO_LARGE,
        )
