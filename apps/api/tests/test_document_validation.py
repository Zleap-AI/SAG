from __future__ import annotations

from types import SimpleNamespace

import pytest

from sag_api.core.errors import ValidationError
from sag_api.services.document_validation import DocumentFileRejection, validate_document_file


@pytest.fixture
def upload_settings() -> SimpleNamespace:
    return SimpleNamespace(allowed_upload_exts={".pdf", ".md"}, max_upload_mb=25)


def test_document_validation_accepts_supported_file(upload_settings: SimpleNamespace) -> None:
    assert validate_document_file("Guide.PDF", 100, upload_settings) is None


@pytest.mark.parametrize(
    ("filename", "size", "message", "reason"),
    [
        ("empty.pdf", 0, "文件内容为空", DocumentFileRejection.EMPTY),
        ("malware.exe", 10, "不支持的文件类型。可上传：md、pdf", DocumentFileRejection.UNSUPPORTED),
        ("README", 10, "不支持的文件类型。可上传：md、pdf", DocumentFileRejection.UNSUPPORTED),
        ("large.pdf", 25 * 1024 * 1024 + 1, "文件超过 25MB 上限", DocumentFileRejection.TOO_LARGE),
    ],
)
def test_document_validation_preserves_upload_messages(
    upload_settings: SimpleNamespace,
    filename: str,
    size: int,
    message: str,
    reason: DocumentFileRejection,
) -> None:
    with pytest.raises(ValidationError, match=message) as captured:
        validate_document_file(filename, size, upload_settings)
    assert captured.value.reason is reason


def test_empty_allowlist_disables_extension_filter(upload_settings: SimpleNamespace) -> None:
    upload_settings.allowed_upload_exts = set()
    validate_document_file("anything.bin", 1, upload_settings)
