"""对话图片附件 —— 上传与取回（本地落盘，鉴权访问）。

仅图片、≤10MB；id = uuid+原始扩展名（正则校验，杜绝路径穿越）。
消息里只存附件 meta（id/media_type/name），发送给视觉模型时由生成层读盘转 base64。
"""

from __future__ import annotations

import os
import re
import uuid

from fastapi import APIRouter, Depends, UploadFile
from fastapi.responses import FileResponse

from sag_api.core.config import settings
from sag_api.core.db import get_session
from sag_api.core.deps import get_current_user
from sag_api.core.errors import NotFoundError, ValidationError
from sag_api.db.models import User

router = APIRouter(prefix="/attachments", tags=["attachments"])

_ALLOWED = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".webp": "image/webp", ".gif": "image/gif"}
_MAX_MB = 10
_ID_RE = re.compile(r"^[0-9a-f]{32}\.(png|jpe?g|webp|gif)$")


def _dir() -> str:
    path = os.path.join(settings.upload_dir, "attachments")
    os.makedirs(path, exist_ok=True)
    return path


def attachment_path(attachment_id: str) -> str | None:
    """id → 磁盘路径（校验失败/不存在返回 None）。供生成层复用。"""
    if not _ID_RE.match(attachment_id or ""):
        return None
    path = os.path.join(_dir(), attachment_id)
    return path if os.path.isfile(path) else None


@router.post("", status_code=201)
async def upload(
    file: UploadFile,
    _user: User = Depends(get_current_user),
    _session=Depends(get_session),
) -> dict:
    ext = os.path.splitext(file.filename or "")[1].lower()
    media_type = _ALLOWED.get(ext)
    if media_type is None:
        raise ValidationError("仅支持图片附件（png / jpg / webp / gif）")
    max_upload_bytes = _MAX_MB * 1024 * 1024
    data = await file.read(max_upload_bytes + 1)
    if len(data) > max_upload_bytes:
        raise ValidationError(f"图片过大（上限 {_MAX_MB}MB）")
    attachment_id = f"{uuid.uuid4().hex}{ext}"
    with open(os.path.join(_dir(), attachment_id), "wb") as f:
        f.write(data)
    return {"id": attachment_id, "name": file.filename or attachment_id, "media_type": media_type}


@router.get("/{attachment_id}")
async def get_file(
    attachment_id: str,
    _user: User = Depends(get_current_user),
) -> FileResponse:
    path = attachment_path(attachment_id)
    if path is None:
        raise NotFoundError("附件不存在")
    ext = os.path.splitext(attachment_id)[1]
    return FileResponse(path, media_type=_ALLOWED.get(ext, "application/octet-stream"))
