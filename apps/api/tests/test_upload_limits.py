"""上傳端點的記憶體容量防護。"""

import pytest

from sag_api.core.errors import ValidationError


class RecordingUpload:
    filename = "oversized.md"
    content_type = "text/markdown"

    def __init__(self) -> None:
        self.read_sizes: list[int] = []

    async def read(self, size: int = -1) -> bytes:
        self.read_sizes.append(size)
        return b"x"


@pytest.mark.asyncio
async def test_document_upload_reads_only_limit_plus_one_byte(monkeypatch):
    from sag_api.api.v1 import documents

    async def get_source(_session, _source_id):
        return object()

    upload = RecordingUpload()
    monkeypatch.setattr(documents, "get_source", get_source)
    monkeypatch.setattr(documents.settings, "max_upload_mb", 0)

    with pytest.raises(ValidationError, match="0MB"):
        await documents.upload(
            "source-id",
            file=upload,
            _user=object(),
            session=object(),
            job_queue=object(),
        )

    assert upload.read_sizes == [1]


@pytest.mark.asyncio
async def test_attachment_upload_reads_only_limit_plus_one_byte(monkeypatch):
    from sag_api.api.v1 import attachments

    upload = RecordingUpload()
    upload.filename = "oversized.png"
    upload.content_type = "image/png"
    monkeypatch.setattr(attachments, "_MAX_MB", 0)

    with pytest.raises(ValidationError, match="0MB"):
        await attachments.upload(
            file=upload,
            _user=object(),
            _session=object(),
        )

    assert upload.read_sizes == [1]
