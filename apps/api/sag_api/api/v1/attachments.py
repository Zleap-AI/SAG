"""对话图片附件 —— 上传与取回路由（本地落盘，鉴权访问）。

落盘与路径解析在 `sag_api.core.attachments`，本模块只负责 HTTP：鉴权、校验、响应。
仅图片、≤10MB；id = uuid+原始扩展名（正则校验，杜绝路径穿越）。
消息里只存附件 meta（id/media_type/name），发送给视觉模型时由生成层读盘转 base64。
"""

from __future__ import annotations

import os
import uuid

from fastapi import APIRouter, Depends, UploadFile
from fastapi.responses import FileResponse

from sag_api.core.attachments import (
    MAX_UPLOAD_MB,
    attachment_file_path,
    attachment_path,
    media_type_for_attachment,
    media_type_for_extension,
)
from sag_api.core.db import get_session
from sag_api.core.deps import get_current_user
from sag_api.core.errors import NotFoundError, ValidationError
from sag_api.db.models import User

router = APIRouter(prefix="/attachments", tags=["attachments"])

# 保留原私有名，供既有测试（test_upload_limits.py monkeypatch）继续生效。
# 扩展名→media_type 的映射不在此重复定义，统一取自 core.attachments。
_MAX_MB = MAX_UPLOAD_MB


@router.post("", status_code=201)
async def upload(
    file: UploadFile,
    _user: User = Depends(get_current_user),
    _session=Depends(get_session),
) -> dict:
    ext = os.path.splitext(file.filename or "")[1].lower()
    media_type = media_type_for_extension(ext)
    if media_type is None:
        raise ValidationError("仅支持图片附件（png / jpg / webp / gif）")
    max_upload_bytes = _MAX_MB * 1024 * 1024
    data = await file.read(max_upload_bytes + 1)
    if len(data) > max_upload_bytes:
        raise ValidationError(f"图片过大（上限 {_MAX_MB}MB）")
    attachment_id = f"{uuid.uuid4().hex}{ext}"
    with open(attachment_file_path(attachment_id), "wb") as f:
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
    return FileResponse(path, media_type=media_type_for_attachment(attachment_id))
