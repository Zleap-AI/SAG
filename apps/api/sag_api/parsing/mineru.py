"""302.AI MinerU 2.x 适配器。

302 的 MinerU 接口只接收公网 PDF URL，因此本地文件先经 `/302/upload-file`
上传，再创建解析任务并轮询。官方文档把成功响应宽泛地声明为 JSON string；
这里同时兼容字符串、嵌套 JSON 和常见对象包装，便于平滑承接服务端响应演进。
"""

from __future__ import annotations

import asyncio
import io
import ipaddress
import json
import os
import re
import socket
import time
import zipfile
from collections.abc import Awaitable, Callable
from typing import Any, Literal, NoReturn
from urllib.parse import urljoin, urlparse

import httpx

from sag_api.core.config import Settings
from sag_api.core.errors import (
    ConfigurationError,
    ServiceUnavailableError,
    UpstreamError,
    ValidationError,
)

StateCallback = Callable[[dict[str, Any]], Awaitable[None]]

_PENDING_STATES = {
    "created",
    "pending",
    "queued",
    "queueing",
    "running",
    "processing",
    "converting",
    "in_progress",
}
_FAILED_STATES = {"failed", "failure", "error", "cancelled", "canceled"}
_DONE_STATES = {"done", "success", "succeeded", "completed", "finished"}
_RESULT_URL_KEYS = (
    "full_zip_url",
    "zip_url",
    "download_url",
    "result_url",
    "markdown_url",
    "md_url",
    "file_url",
)
_MARKDOWN_KEYS = ("markdown", "md_content", "markdown_content", "content")


class MinerUClient:
    def __init__(self, settings: Settings):
        if not settings.mineru_configured:
            raise ConfigurationError("MinerU 尚未配置 Base URL 与 API Key")
        self._base_url = str(settings.mineru_base_url).rstrip("/")
        parsed_base = urlparse(self._base_url)
        if parsed_base.scheme not in {"http", "https"} or not parsed_base.hostname:
            raise ConfigurationError("MinerU Base URL 必须是有效的 HTTP(S) 地址")
        if parsed_base.hostname in {"api.302.ai", "api.302ai.cn"} and parsed_base.scheme != "https":
            raise ConfigurationError("302 MinerU Base URL 必须使用 HTTPS")
        self._api_key = str(settings.mineru_api_key)
        self._version = settings.mineru_version
        self._parse_method = settings.mineru_parse_method
        self._request_timeout = max(1.0, settings.mineru_request_timeout)
        self._poll_interval = max(0.05, settings.mineru_poll_interval)
        self._poll_timeout = max(self._poll_interval, settings.mineru_poll_timeout)
        self._result_limit = max(1, settings.mineru_result_max_mb) * 1024 * 1024

    @property
    def signature(self) -> str:
        return f"mineru-{self._version}-{self._parse_method}"

    def _url(self, path: str) -> str:
        return f"{self._base_url}/{path.lstrip('/')}"

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}"}

    async def parse(
        self,
        path: str,
        *,
        state: dict[str, Any] | None = None,
        on_state: StateCallback | None = None,
    ) -> str:
        state = dict(state or {})
        upload_url = state.get("upload_url")
        if not isinstance(upload_url, str) or not _is_http_url(upload_url):
            upload_url = await self._upload(path)
            state["upload_url"] = upload_url
            if on_state:
                await on_state(dict(state))

        task_id = state.get("task_id")
        if not isinstance(task_id, str) or not task_id.strip():
            created_kind, created_value = await self._create_task(upload_url)
            if created_kind == "url":
                markdown = await self._download_markdown(created_value)
                if on_state:
                    await on_state({**state, "status": "done"})
                return markdown
            if created_kind == "markdown":
                if on_state:
                    await on_state({**state, "status": "done"})
                return _require_markdown(created_value)
            task_id = created_value
            state["task_id"] = task_id
            if on_state:
                await on_state(dict(state))

        markdown = await self._poll(str(task_id))
        if on_state:
            await on_state({**state, "status": "done"})
        return markdown

    async def _upload(self, path: str) -> str:
        try:
            if os.path.getsize(path) > 50 * 1024 * 1024:
                raise ValidationError("302 文件上传接口限制 PDF 不得超过 50MB")
        except OSError as exc:
            raise UpstreamError(f"无法读取待解析 PDF：{exc}") from exc
        try:
            async with httpx.AsyncClient(timeout=self._request_timeout) as client:
                with open(path, "rb") as source:
                    response = await client.post(
                        self._url("/302/upload-file"),
                        headers=self._headers,
                        files={"file": (os.path.basename(path), source, "application/pdf")},
                    )
        except OSError as exc:
            raise UpstreamError(f"无法读取待解析 PDF：{exc}") from exc
        except httpx.TimeoutException as exc:
            raise ServiceUnavailableError("上传 PDF 到 302 超时") from exc
        except httpx.RequestError as exc:
            raise ServiceUnavailableError(f"无法上传 PDF 到 302：{exc}") from exc
        response = self._checked(response, "上传 PDF")
        payload = _response_payload(response)
        upload_url = _find_http_url(
            payload, preferred=("data", "file_url", "url", "download_url")
        )
        if not upload_url:
            raise UpstreamError("302 文件上传成功，但响应中没有文件 URL")
        return upload_url

    async def _create_task(
        self, upload_url: str
    ) -> tuple[Literal["task", "url", "markdown"], str]:
        response = await self._api_request(
            "POST",
            "/302/v2/mineru/task",
            json={
                "pdf_url": upload_url,
                "parse_method": self._parse_method,
                "version": self._version,
            },
        )
        payload = _response_payload(response)
        if isinstance(payload, str):
            kind, value = _interpret_poll_payload(payload, "")
            if kind in {"url", "markdown"}:
                return kind, value
            if kind == "failed":
                raise UpstreamError(f"MinerU 创建任务失败：{value}")
        task_id = _find_task_id(payload)
        if task_id:
            return "task", task_id
        kind, value = _interpret_poll_payload(payload, "")
        if kind in {"url", "markdown"}:
            return kind, value
        if kind == "failed":
            raise UpstreamError(f"MinerU 创建任务失败：{value}")
        raise UpstreamError("MinerU 已接受请求，但响应中没有任务 ID")

    async def _poll(self, task_id: str) -> str:
        deadline = time.monotonic() + self._poll_timeout
        while True:
            response = await self._api_request(
                "GET", "/302/v2/mineru/task", params={"task_id": task_id}
            )
            kind, value = _interpret_poll_payload(_response_payload(response), task_id)
            if kind == "markdown":
                return _require_markdown(value)
            if kind == "url":
                return await self._download_markdown(value)
            if kind == "failed":
                raise UpstreamError(f"MinerU 解析失败：{value}")
            if time.monotonic() >= deadline:
                raise ServiceUnavailableError(
                    f"MinerU 解析等待超时（任务 {task_id}），后台将继续重试"
                )
            await asyncio.sleep(self._poll_interval)

    async def _download_markdown(self, url: str) -> str:
        # 结果地址通常位于 file.302.ai；不向第三方下载地址转发 API Key。
        try:
            async with httpx.AsyncClient(timeout=self._request_timeout) as client:
                current_url = url
                for redirect_count in range(6):
                    await self._validate_result_url(
                        current_url, require_official_host=redirect_count == 0
                    )
                    async with client.stream(
                        "GET", current_url, follow_redirects=False
                    ) as response:
                        if response.is_redirect and response.headers.get("location"):
                            current_url = urljoin(current_url, response.headers["location"])
                            continue
                        content = await self._read_result_response(response)
                        content_type = response.headers.get("content-type", "").lower()
                        response_url = str(response.url)
                        encoding = response.encoding or "utf-8"
                        break
                else:
                    raise UpstreamError("MinerU 结果下载重定向次数过多")
        except httpx.TimeoutException as exc:
            raise ServiceUnavailableError("下载 MinerU 解析结果超时") from exc
        except httpx.RequestError as exc:
            raise ServiceUnavailableError(f"无法下载 MinerU 解析结果：{exc}") from exc

        suffix = os.path.splitext(urlparse(response_url).path)[1].lower()
        if content.startswith(b"PK\x03\x04") or suffix == ".zip" or "zip" in content_type:
            return _markdown_from_zip(content, self._result_limit)

        text = content.decode(encoding, errors="replace")
        if "json" in content_type or suffix == ".json":
            try:
                kind, value = _interpret_poll_payload(json.loads(text), "")
            except json.JSONDecodeError:
                pass
            else:
                if kind == "markdown":
                    return _require_markdown(value)
                if kind == "url" and value != url:
                    return await self._download_markdown(value)
                if kind == "failed":
                    raise UpstreamError(f"MinerU 解析失败：{value}")
        return _require_markdown(text)

    async def _read_result_response(self, response: httpx.Response) -> bytes:
        if not response.is_success:
            error_body = bytearray()
            async for chunk in response.aiter_bytes():
                error_body.extend(chunk)
                if len(error_body) >= min(self._result_limit, 64 * 1024):
                    break
            self._raise_status(
                response.status_code,
                "下载解析结果",
                _error_message_bytes(bytes(error_body)),
            )
        declared = response.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > self._result_limit:
            raise UpstreamError("MinerU 解析结果超过允许大小")
        chunks = bytearray()
        async for chunk in response.aiter_bytes():
            chunks.extend(chunk)
            if len(chunks) > self._result_limit:
                raise UpstreamError("MinerU 解析结果超过允许大小")
        return bytes(chunks)

    async def _validate_result_url(self, url: str, *, require_official_host: bool) -> None:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower().rstrip(".")
        if parsed.scheme not in {"http", "https"} or not host or parsed.username or parsed.password:
            raise UpstreamError("MinerU 返回了不安全的结果 URL")
        try:
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
        except ValueError as exc:
            raise UpstreamError("MinerU 返回了不安全的结果 URL") from exc

        base_host = (urlparse(self._base_url).hostname or "").lower().rstrip(".")
        official_base = base_host in {"api.302.ai", "api.302ai.cn"}
        official_result = host in {"302.ai", "302ai.cn"} or host.endswith(
            (".302.ai", ".302ai.cn")
        )
        if require_official_host and official_base:
            if parsed.scheme != "https" or not official_result:
                raise UpstreamError("302 MinerU 返回了不受信任的结果 URL")
            return
        await _assert_public_host(host, port)

    async def _api_request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            async with httpx.AsyncClient(timeout=self._request_timeout) as client:
                response = await client.request(
                    method, self._url(path), headers=self._headers, **kwargs
                )
        except httpx.TimeoutException as exc:
            raise ServiceUnavailableError("MinerU 请求超时") from exc
        except httpx.RequestError as exc:
            raise ServiceUnavailableError(f"无法连接 MinerU：{exc}") from exc
        return self._checked(response, "调用 MinerU")

    @staticmethod
    def _checked(response: httpx.Response, action: str) -> httpx.Response:
        if response.is_success:
            return response
        message = _error_message(response)
        MinerUClient._raise_status(response.status_code, action, message)

    @staticmethod
    def _raise_status(status_code: int, action: str, message: str) -> NoReturn:
        if status_code in {401, 403}:
            raise ConfigurationError(f"{action}鉴权失败，请检查 MinerU API Key")
        if status_code == 429 or status_code >= 500:
            raise ServiceUnavailableError(f"{action}暂时不可用（{status_code}）：{message}")
        raise UpstreamError(f"{action}失败（{status_code}）：{message}")


def _response_payload(response: httpx.Response) -> Any:
    try:
        payload: Any = response.json()
    except ValueError:
        payload = response.text
    return _unwrap_json(payload)


def _unwrap_json(payload: Any) -> Any:
    for _ in range(4):
        if not isinstance(payload, str):
            break
        value = payload.strip()
        if not value or value[0] not in "[{\"":
            break
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            break
        if decoded == payload:
            break
        payload = decoded
    return payload


def _find_task_id(payload: Any) -> str | None:
    payload = _unwrap_json(payload)
    if isinstance(payload, str):
        value = payload.strip()
        return value if value and not _is_http_url(value) and not _looks_like_markdown(value) else None
    if isinstance(payload, dict):
        for key in ("task_id", "taskId", "request_id", "id"):
            value = payload.get(key)
            if isinstance(value, (str, int)) and str(value).strip():
                return str(value).strip()
        for key in ("data", "result", "task"):
            if key in payload:
                found = _find_task_id(payload[key])
                if found:
                    return found
    return None


def _interpret_poll_payload(
    payload: Any, task_id: str
) -> tuple[Literal["pending", "failed", "url", "markdown"], str]:
    payload = _unwrap_json(payload)
    if isinstance(payload, str):
        value = payload.strip()
        lower = value.lower().replace("-", "_").replace(" ", "_")
        if _is_http_url(value):
            return "url", value
        if lower in _PENDING_STATES or value == task_id:
            return "pending", value
        if lower in _FAILED_STATES or any(
            token in lower for token in ("failed", "error", "失败")
        ):
            return "failed", value or "未知错误"
        if re.fullmatch(r"(?:A\d{4}|-\d{5})", value, flags=re.IGNORECASE):
            return "failed", f"错误码 {value}"
        if _looks_like_markdown(value):
            return "markdown", value
        return "pending", value

    if isinstance(payload, dict):
        status = _find_nested_string(
            payload, ("status", "state", "task_status", "taskState")
        )
        normalized = (status or "").lower().replace("-", "_").replace(" ", "_")
        code = payload.get("code")
        if code is not None and str(code).lower() not in {"0", "200", "ok", "success"}:
            message = _find_error_message(payload)
            return "failed", message or f"错误码 {code}"
        if normalized in _FAILED_STATES:
            error = _find_error_message(payload)
            return "failed", error or status or "未知错误"
        if normalized in _PENDING_STATES:
            return "pending", status or "pending"

        url = _find_http_url(payload, preferred=_RESULT_URL_KEYS)
        if not url and normalized in _DONE_STATES:
            url = _find_http_url(payload, preferred=("url",))
        if not url and not status:
            url = _url_from_result_wrapper(payload)
        if url:
            return "url", url
        markdown = _find_markdown(payload)
        if markdown:
            return "markdown", markdown

        if normalized in _DONE_STATES:
            return "failed", "任务已完成，但响应中没有 Markdown 或下载地址"
        return "pending", status or "pending"

    if isinstance(payload, list):
        for item in payload:
            kind, value = _interpret_poll_payload(item, task_id)
            if kind != "pending":
                return kind, value
    return "pending", "pending"


def _find_http_url(payload: Any, *, preferred: tuple[str, ...]) -> str | None:
    payload = _unwrap_json(payload)
    if isinstance(payload, str):
        value = payload.strip()
        return value if _is_http_url(value) else None
    if isinstance(payload, dict):
        for key in preferred:
            if key in payload:
                found = _find_http_url(payload[key], preferred=preferred)
                if found:
                    return found
        # 继续进入对象/数组寻找嵌套的目标键，但不把诸如输入 pdf_url 之类
        # 的任意字符串误判为解析结果。
        for value in payload.values():
            if isinstance(value, (dict, list)):
                found = _find_http_url(value, preferred=preferred)
                if found:
                    return found
    if isinstance(payload, list):
        for value in payload:
            found = _find_http_url(value, preferred=preferred)
            if found:
                return found
    return None


def _find_markdown(payload: Any) -> str | None:
    if isinstance(payload, dict):
        for key in _MARKDOWN_KEYS:
            value = _unwrap_json(payload.get(key))
            if isinstance(value, str) and _looks_like_markdown(value):
                return value.strip()
        for value in payload.values():
            if isinstance(value, (dict, list)):
                found = _find_markdown(value)
                if found:
                    return found
    elif isinstance(payload, list):
        for value in payload:
            found = _find_markdown(value)
            if found:
                return found
    return None


def _url_from_result_wrapper(payload: dict[str, Any]) -> str | None:
    for key in ("data", "result", "output"):
        value = _unwrap_json(payload.get(key))
        if isinstance(value, str) and _is_http_url(value.strip()):
            return value.strip()
        if isinstance(value, dict):
            nested = _url_from_result_wrapper(value)
            if nested:
                return nested
    return None


def _find_error_message(payload: Any) -> str | None:
    for key in ("err_msg", "error", "detail", "message", "msg"):
        value = _find_key_string(payload, key)
        if value and value.lower() not in {"ok", "success", "succeeded"}:
            return value
    return None


def _find_key_string(payload: Any, key: str) -> str | None:
    if isinstance(payload, dict):
        value = payload.get(key)
        if isinstance(value, (str, int, float)) and str(value).strip():
            return str(value).strip()
        for child in payload.values():
            if isinstance(child, (dict, list)):
                found = _find_key_string(child, key)
                if found:
                    return found
    elif isinstance(payload, list):
        for child in payload:
            found = _find_key_string(child, key)
            if found:
                return found
    return None


def _find_nested_string(payload: Any, keys: tuple[str, ...]) -> str | None:
    if isinstance(payload, dict):
        for key in keys:
            value = payload.get(key)
            if isinstance(value, (str, int, float)) and str(value).strip():
                return str(value).strip()
        for value in payload.values():
            if isinstance(value, (dict, list)):
                found = _find_nested_string(value, keys)
                if found:
                    return found
    elif isinstance(payload, list):
        for value in payload:
            found = _find_nested_string(value, keys)
            if found:
                return found
    return None


def _looks_like_markdown(value: str) -> bool:
    text = value.strip()
    return bool(text) and ("\n" in text or text.startswith(("# ", "## ", "---\n")))


def _is_http_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


async def _assert_public_host(host: str, port: int) -> None:
    """拒绝 loopback、私网、链路本地与保留地址，降低结果下载 SSRF 风险。"""
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        raise UpstreamError("MinerU 结果 URL 指向了本地或内网地址")
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        try:
            records = await asyncio.to_thread(
                socket.getaddrinfo, host, port, type=socket.SOCK_STREAM
            )
        except OSError as exc:
            raise ServiceUnavailableError(f"无法解析 MinerU 结果域名：{host}") from exc
        addresses = {record[4][0] for record in records}
        if not addresses:
            raise ServiceUnavailableError(f"无法解析 MinerU 结果域名：{host}") from None
        resolved = [ipaddress.ip_address(address) for address in addresses]
    else:
        resolved = [literal]
    if any(not address.is_global for address in resolved):
        raise UpstreamError("MinerU 结果 URL 指向了本地或内网地址")


def _require_markdown(value: str) -> str:
    markdown = value.strip()
    if not markdown:
        raise UpstreamError("文档解析结果为空")
    return markdown + "\n"


def _markdown_from_zip(content: bytes, size_limit: int) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            candidates = [
                info
                for info in archive.infolist()
                if not info.is_dir() and info.filename.lower().endswith((".md", ".markdown"))
            ]
            if not candidates:
                raise UpstreamError("MinerU 结果压缩包中没有 Markdown 文件")
            candidates.sort(
                key=lambda info: (
                    os.path.basename(info.filename).lower() not in {"full.md", "full.markdown"},
                    -info.file_size,
                )
            )
            chosen = candidates[0]
            if chosen.file_size > size_limit:
                raise UpstreamError("MinerU Markdown 结果超过允许大小")
            return _require_markdown(archive.read(chosen).decode("utf-8", errors="replace"))
    except zipfile.BadZipFile as exc:
        raise UpstreamError("MinerU 返回的结果压缩包已损坏") from exc


def _error_message(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return (response.text or response.reason_phrase).strip()[:300]
    if isinstance(payload, dict):
        message = _find_error_message(payload)
        if message:
            return message[:300]
    return str(payload)[:300]


def _error_message_bytes(content: bytes) -> str:
    text = content.decode("utf-8", errors="replace").strip()
    if not text:
        return "空响应"
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return text[:300]
    if isinstance(payload, dict):
        message = _find_error_message(payload)
        if message:
            return message[:300]
    return str(payload)[:300]
