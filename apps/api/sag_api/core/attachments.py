"""对话图片附件的落盘与取回 —— 与 HTTP 无关的存储层。

仅图片、≤10MB；id = uuid+原始扩展名（正则校验，杜绝路径穿越）。
API 层（`api/v1/attachments.py`）只做路由与鉴权，消息落库（`services/agent_domain.py`）
与生成层（`generation/prompt.py`）都从这里取磁盘路径，依赖方向单向向下：
API → core，services → core，避免服务层反向 import 路由模块。
"""

from __future__ import annotations

import os
import re

from sag_api.core.config import settings

# 扩展名 → media_type，upload / 取回 / 消息 meta 三处共用一份，避免各自推导漂移
ALLOWED_MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}
MAX_UPLOAD_MB = 10
ATTACHMENT_ID_RE = re.compile(r"^[0-9a-f]{32}\.(png|jpe?g|webp|gif)$")


def attachments_dir() -> str:
    """附件目录（不存在则创建）。"""
    path = os.path.join(settings.upload_dir, "attachments")
    os.makedirs(path, exist_ok=True)
    return path


def attachment_path(attachment_id: str) -> str | None:
    """id → 磁盘路径（校验失败/不存在返回 None）。供生成层复用。"""
    if not ATTACHMENT_ID_RE.match(attachment_id or ""):
        return None
    path = os.path.join(attachments_dir(), attachment_id)
    return path if os.path.isfile(path) else None


def media_type_for_extension(extension: str) -> str | None:
    """扩展名（含点，大小写不敏感）→ media_type；不支持则 None。"""
    return ALLOWED_MEDIA_TYPES.get((extension or "").lower())


def media_type_for_attachment(attachment_id: str) -> str:
    """附件 id → media_type（兜底 octet-stream）。"""
    return ALLOWED_MEDIA_TYPES.get(os.path.splitext(attachment_id or "")[1].lower(), "application/octet-stream")


def attachment_file_path(attachment_id: str) -> str:
    """写入前的目标路径，不校验存在性（供上传落盘使用）。"""
    return os.path.join(attachments_dir(), attachment_id)
