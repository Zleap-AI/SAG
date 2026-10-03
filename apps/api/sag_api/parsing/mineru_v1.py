"""自部署 MinerU 4.x V1 HTTP API 适配器。

协议参照 MinerU 4.0 官方 `docs/usage/http_api.md` 与 `scripts/http_api_example.sh`：
创建上传 → PUT 原始字节 → 完成上传 → 提交解析任务 → 轮询终态 → 经
`/v1/files/{file_id}/content` 下载 zip 结果。自部署服务常位于本机或内网，
因此只有显式开启 `mineru_allow_private_base_url` 时，才放行用户配置的 Base URL
指向内网/loopback；结果下载与带鉴权的上传必须与 Base URL 同源。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import mimetypes
import os
import time
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

from sag_api.core.config import Settings
from sag_api.core.errors import (
    ConfigurationError,
    ServiceUnavailableError,
    UpstreamError,
)
from sag_api.parsing import mineru as mineru_core
from sag_api.parsing.mineru import MinerU302Client, ParsePaused, PauseCallback, StateCallback
from sag_api.parsing.mineru_artifacts import MinerUResult, result_from_zip
from sag_api.parsing.mineru_official import _file_chunks

log = logging.getLogger(__name__)

_PENDING_JOB_STATES = {"queued", "running"}
_TERMINAL_JOB_STATES = {"completed", "partial", "failed", "canceled"}
_JOB_STATES = _PENDING_JOB_STATES | _TERMINAL_JOB_STATES
_RESUME_KEYS = ("file_id", "job_id")


class _ResourceLost(Exception):
    """服务重启后进程内的 upload/file/job 记录失效（V1 API 不持久化任务）。"""


class SelfHostedMinerUClient(MinerU302Client):
    def __init__(self, settings: Settings):
        super().__init__(settings)
        self._api_root = _v1_api_root(self._base_url)
        self._api_key = settings.mineru_api_key or ""
        self._tier = settings.mineru_tier
        self._allow_private = settings.mineru_allow_private_base_url

    @property
    def signature(self) -> str:
        return f"mineru-v1-{self._tier or 'default'}-{self._parse_method}"

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}

    async def parse_result(
        self,
        path: str,
        *,
        state: dict[str, Any] | None = None,
        on_state: StateCallback | None = None,
        should_pause: PauseCallback | None = None,
    ) -> MinerUResult:
        current = dict(state or {})
        current["mineru_service"] = "self_hosted"
        for attempt in range(2):
            try:
                result = await self._run(
                    path, current, on_state=on_state, should_pause=should_pause
                )
            except _ResourceLost as exc:
                if attempt:
                    raise UpstreamError(f"MinerU 自部署服务找不到任务资源：{exc}") from None
                log.warning("MinerU 自部署服务资源已失效，重新提交：%s", exc)
                for key in _RESUME_KEYS:
                    current.pop(key, None)
                current.pop("status", None)
                if on_state:
                    await on_state(dict(current))
                continue
            if on_state:
                await on_state({**current, "status": "done"})
            return result
        raise UpstreamError("MinerU 自部署服务任务重试失败")  # pragma: no cover

    async def _run(
        self,
        path: str,
        current: dict[str, Any],
        *,
        on_state: StateCallback | None,
        should_pause: PauseCallback | None,
    ) -> MinerUResult:
        job_id = current.get("job_id")
        if not _non_empty(job_id):
            file_id = current.get("file_id")
            if not _non_empty(file_id):
                file_id = await self._upload(path, current, on_state=on_state)
            job_id = await self._create_job(str(file_id))
            current["job_id"] = job_id
            if on_state:
                await on_state(dict(current))
        result_file_id = await self._poll_job(
            str(job_id), current, on_state=on_state, should_pause=should_pause
        )
        return await self._download_zip(result_file_id)

    async def _upload(
        self,
        path: str,
        current: dict[str, Any],
        *,
        on_state: StateCallback | None,
    ) -> str:
        try:
            size = os.path.getsize(path)
            digest = await asyncio.to_thread(_sha256_file, path)
        except OSError as exc:
            raise UpstreamError(f"无法读取待解析文件：{exc}") from exc
        filename = os.path.basename(path)
        mime_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        payload = await self._v1_json(
            "POST",
            "uploads",
            "创建上传",
            json={
                "filename": filename,
                "bytes": size,
                "mime_type": mime_type,
                "purpose": "parse",
                "sha256sum": digest,
            },
        )
        upload_id = _required_string(payload, "id", "创建上传")
        status = payload.get("status")
        if status == "pending":
            await self._put_bytes(path, payload)
            payload = await self._v1_json(
                "POST",
                f"uploads/{upload_id}/complete",
                "完成上传",
                json={"sha256sum": digest},
            )
            status = payload.get("status")
        if status != "completed":
            raise UpstreamError(f"MinerU 上传状态异常：{status or '空'}")
        file_info = payload.get("file")
        file_id = _required_string(file_info if isinstance(file_info, dict) else {}, "id", "上传")
        current["file_id"] = file_id
        if on_state:
            await on_state(dict(current))
        return file_id

    async def _put_bytes(self, path: str, upload: dict[str, Any]) -> None:
        upload_url = upload.get("upload_url")
        method = upload.get("upload_method") or "PUT"
        if not _non_empty(upload_url) or method != "PUT":
            raise UpstreamError("MinerU 上传响应缺少有效的 PUT 上传地址")
        target = urljoin(f"{self._api_root}/", str(upload_url))
        headers = upload.get("upload_headers") or {}
        if not isinstance(headers, dict) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in headers.items()
        ):
            raise UpstreamError("MinerU 上传响应中的 upload_headers 无效")
        headers = dict(headers)
        if _same_origin(target, self._api_root):
            await self._check_base_host()
            headers.update(self._headers)
        else:
            # 跨源上传（如对象存储签名地址）自带授权，不转发 MinerU Key，并沿用公网校验。
            parsed = _checked_http_url(target, "MinerU 返回了不安全的上传地址")
            await mineru_core._assert_public_host(parsed.hostname or "", _effective_port(parsed))
        try:
            async with httpx.AsyncClient(timeout=self._request_timeout) as client:
                response = await client.request(
                    "PUT", target, headers=headers, content=_file_chunks(path)
                )
        except OSError as exc:
            raise UpstreamError(f"无法读取待解析文件：{exc}") from exc
        except httpx.TimeoutException as exc:
            raise ServiceUnavailableError("上传文件到 MinerU 超时") from exc
        except httpx.RequestError as exc:
            raise ServiceUnavailableError(f"无法上传文件到 MinerU：{exc}") from exc
        self._checked(response, "上传文件到 MinerU")

    async def _create_job(self, file_id: str) -> str:
        body: dict[str, Any] = {
            "files": [{"source": {"type": "file_id", "file_id": file_id}}],
            "ocr_mode": self._parse_method,
            "output_formats": ["zip"],
        }
        if self._tier:
            body["tier"] = self._tier
        payload = await self._v1_json(
            "POST", "parse/jobs", "提交解析任务", json=body, lost_on_404=True
        )
        job_id = _required_string(payload, "job_id", "提交解析任务")
        log.info("MinerU 自部署服务已创建解析任务 job=%s", job_id)
        return job_id

    async def _poll_job(
        self,
        job_id: str,
        current: dict[str, Any],
        *,
        on_state: StateCallback | None,
        should_pause: PauseCallback | None,
    ) -> str:
        deadline = time.monotonic() + self._poll_timeout
        last_status: str | None = None
        while True:
            if should_pause is not None and await should_pause():
                log.info("MinerU 轮询收到暂停信号，协作式中断 job=%s", job_id)
                raise ParsePaused()
            payload = await self._v1_json(
                "GET", f"parse/jobs/{job_id}", "查询解析任务", lost_on_404=True
            )
            status = payload.get("status")
            if status not in _JOB_STATES:
                raise UpstreamError(f"MinerU 返回未知任务状态：{status or '空'}")
            if status in _TERMINAL_JOB_STATES:
                return _completed_zip_file_id(payload, str(status))
            if status != last_status:
                last_status = str(status)
                if on_state is not None:
                    await on_state({**current, "status": last_status})
            if time.monotonic() >= deadline:
                raise ServiceUnavailableError(f"MinerU 解析等待超时（任务 {job_id}）")
            await asyncio.sleep(self._poll_interval)

    async def _download_zip(self, file_id: str) -> MinerUResult:
        current_url = f"{self._api_root}/v1/files/{file_id}/content"
        try:
            async with httpx.AsyncClient(timeout=self._request_timeout) as client:
                for _ in range(6):
                    if not _same_origin(current_url, self._api_root):
                        raise UpstreamError("MinerU 结果下载地址与 Base URL 不同源")
                    await self._check_base_host()
                    async with client.stream(
                        "GET", current_url, headers=self._headers, follow_redirects=False
                    ) as response:
                        if response.is_redirect and response.headers.get("location"):
                            current_url = urljoin(current_url, response.headers["location"])
                            continue
                        content = await self._read_result_response(response)
                        break
                else:
                    raise UpstreamError("MinerU 结果下载重定向次数过多")
        except httpx.TimeoutException as exc:
            raise ServiceUnavailableError("下载 MinerU 解析结果超时") from exc
        except httpx.RequestError as exc:
            raise ServiceUnavailableError(f"无法下载 MinerU 解析结果：{exc}") from exc
        return result_from_zip(content, self._result_limit)

    async def _v1_json(
        self,
        method: str,
        path: str,
        action: str,
        *,
        lost_on_404: bool = False,
        **kwargs: Any,
    ) -> dict[str, Any]:
        await self._check_base_host()
        url = f"{self._api_root}/v1/{path.lstrip('/')}"
        try:
            async with httpx.AsyncClient(timeout=self._request_timeout) as client:
                response = await client.request(method, url, headers=self._headers, **kwargs)
        except httpx.TimeoutException as exc:
            raise ServiceUnavailableError(f"MinerU {action}请求超时") from exc
        except httpx.RequestError as exc:
            raise ServiceUnavailableError(f"无法连接 MinerU 自部署服务：{exc}") from exc
        if lost_on_404 and response.status_code == 404:
            raise _ResourceLost(mineru_core._error_message(response))
        payload = mineru_core._response_payload(self._checked(response, f"MinerU {action}"))
        if not isinstance(payload, dict):
            raise UpstreamError(f"MinerU {action}响应格式无效")
        return payload

    async def _check_base_host(self) -> None:
        if self._allow_private:
            return
        parsed = urlparse(self._api_root)
        try:
            await mineru_core._assert_public_host(parsed.hostname or "", _effective_port(parsed))
        except UpstreamError as exc:
            raise ConfigurationError(
                "MinerU 自部署 Base URL 指向本机或内网地址；确认可信后请设置 "
                "SAG_MINERU_ALLOW_PRIVATE_BASE_URL=true"
            ) from exc


def _v1_api_root(base_url: str) -> str:
    parsed = urlparse(base_url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ConfigurationError("MinerU 自部署 Base URL 必须是有效的 HTTP(S) 地址")
    try:
        _ = parsed.port
    except ValueError as exc:
        raise ConfigurationError("MinerU 自部署 Base URL 端口无效") from exc
    path = parsed.path.rstrip("/")
    if path.endswith("/v1"):
        path = path[: -len("/v1")]
    return f"{parsed.scheme}://{parsed.netloc}{path}"


def _checked_http_url(url: str, message: str):  # noqa: ANN202 - urllib ParseResult
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise UpstreamError(message)
    try:
        _ = parsed.port
    except ValueError as exc:
        raise UpstreamError(message) from exc
    return parsed


def _effective_port(parsed) -> int:  # noqa: ANN001 - urllib ParseResult
    return parsed.port or (443 if parsed.scheme == "https" else 80)


def _same_origin(url: str, base: str) -> bool:
    """按 scheme + host + 有效端口比较，与 MinerU SDK 的同源规则一致。"""
    target = urlparse(url)
    origin = urlparse(base)
    try:
        return (
            target.scheme.lower() == origin.scheme.lower()
            and (target.hostname or "").lower() == (origin.hostname or "").lower()
            and _effective_port(target) == _effective_port(origin)
            and not target.username
            and not target.password
        )
    except ValueError:
        return False


def _completed_zip_file_id(payload: dict[str, Any], status: str) -> str:
    files = payload.get("files")
    entry = files[0] if isinstance(files, list) and files and isinstance(files[0], dict) else {}
    if status == "completed" or (status == "partial" and entry.get("status") == "completed"):
        outputs = entry.get("output_files")
        zip_ref = outputs.get("zip") if isinstance(outputs, dict) else None
        file_id = zip_ref.get("file_id") if isinstance(zip_ref, dict) else None
        if not _non_empty(file_id):
            raise UpstreamError("MinerU 任务已完成，但响应中没有 zip 结果")
        return str(file_id)
    message = mineru_core._find_error_message(entry.get("error") or {}) or "未知错误"
    if status == "canceled":
        raise UpstreamError(f"MinerU 解析任务已取消：{message}")
    raise UpstreamError(f"MinerU 解析失败：{message}")


def _required_string(payload: dict[str, Any], key: str, action: str) -> str:
    value = payload.get(key)
    if not _non_empty(value):
        raise UpstreamError(f"MinerU {action}响应中没有 {key}")
    return str(value).strip()


def _non_empty(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()
