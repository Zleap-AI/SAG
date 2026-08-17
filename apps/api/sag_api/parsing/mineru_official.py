"""MinerU 官方 v4 精准解析 API 适配器。"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import urlparse

import httpx

from sag_api.core.config import Settings
from sag_api.core.errors import (
    ConfigurationError,
    ServiceUnavailableError,
    UpstreamError,
    ValidationError,
)
from sag_api.parsing import mineru as mineru_core
from sag_api.parsing.mineru import MinerU302Client, StateCallback

_PENDING_STATES = {"waiting-file", "pending", "running", "converting"}


class OfficialMinerUClient(MinerU302Client):
    def __init__(self, settings: Settings):
        super().__init__(settings)
        self._api_root = _official_api_root(self._base_url)
        self._official_model = settings.mineru_official_model

    @property
    def signature(self) -> str:
        return (
            f"mineru-official-{self._official_model}-{self._parse_method}"
        )

    async def parse(
        self,
        path: str,
        *,
        state: dict[str, Any] | None = None,
        on_state: StateCallback | None = None,
    ) -> str:
        filename = os.path.basename(path)
        current = dict(state or {})
        saved_filename = current.get("filename")
        if isinstance(saved_filename, str) and saved_filename:
            filename = saved_filename

        batch_id = current.get("batch_id")
        upload_url = current.get("upload_url")
        upload_completed = current.get("upload_completed") is True

        if not isinstance(batch_id, str) or not batch_id.strip():
            batch_id, upload_url = await self._request_upload(filename)
            current.update(
                {
                    "mineru_service": "official",
                    "batch_id": batch_id,
                    "filename": filename,
                    "upload_url": upload_url,
                    "upload_completed": False,
                }
            )
            if on_state:
                await on_state(dict(current))

        if not upload_completed:
            if not isinstance(upload_url, str) or not upload_url:
                raise UpstreamError(
                    "MinerU 官方任务缺少待续传的签名上传地址"
                )
            await self._upload_file(path, upload_url)
            current["upload_completed"] = True
            current.pop("upload_url", None)
            if on_state:
                await on_state(dict(current))

        result_url = await self._poll_batch(batch_id, filename)
        markdown = await self._download_markdown(result_url)
        if on_state:
            await on_state({**current, "status": "done"})
        return markdown

    async def _request_upload(self, filename: str) -> tuple[str, str]:
        file_config: dict[str, Any] = {"name": filename}
        if self._parse_method == "txt":
            file_config["is_ocr"] = False
        elif self._parse_method == "ocr":
            file_config["is_ocr"] = True
        response = await self._official_request(
            "POST",
            "file-urls/batch",
            json={
                "files": [file_config],
                "model_version": self._official_model,
            },
        )
        data = _official_data(response, "申请上传地址")
        batch_id = data.get("batch_id")
        upload_urls = data.get("file_urls")
        if not isinstance(batch_id, str) or not batch_id.strip():
            raise UpstreamError("MinerU 官方响应中没有 batch_id")
        if (
            not isinstance(upload_urls, list)
            or len(upload_urls) != 1
            or not isinstance(upload_urls[0], str)
            or not upload_urls[0]
        ):
            raise UpstreamError("MinerU 官方响应中没有唯一的签名上传地址")
        return batch_id.strip(), upload_urls[0]

    async def _upload_file(self, path: str, upload_url: str) -> None:
        try:
            if os.path.getsize(path) > 200 * 1024 * 1024:
                raise ValidationError("MinerU 官方接口限制文件不得超过 200MB")
        except OSError as exc:
            raise UpstreamError(f"无法读取待解析 PDF：{exc}") from exc
        await _validate_signed_url(upload_url)
        try:
            async with httpx.AsyncClient(timeout=self._request_timeout) as client:
                response = await client.request(
                    "PUT",
                    upload_url,
                    content=_file_chunks(path),
                )
        except OSError as exc:
            raise UpstreamError(f"无法读取待解析 PDF：{exc}") from exc
        except httpx.TimeoutException as exc:
            raise ServiceUnavailableError("上传 PDF 到 MinerU 官方服务超时") from exc
        except httpx.RequestError as exc:
            raise ServiceUnavailableError(
                f"无法上传 PDF 到 MinerU 官方服务：{exc}"
            ) from exc
        self._checked(response, "上传 PDF 到 MinerU 官方服务")

    async def _poll_batch(self, batch_id: str, filename: str) -> str:
        deadline = time.monotonic() + self._poll_timeout
        while True:
            response = await self._official_request(
                "GET", f"extract-results/batch/{batch_id}"
            )
            data = _official_data(response, "查询解析任务")
            results = data.get("extract_result")
            if not isinstance(results, list):
                raise UpstreamError("MinerU 官方响应中没有 extract_result")
            result = next(
                (
                    item
                    for item in results
                    if isinstance(item, dict) and item.get("file_name") == filename
                ),
                None,
            )
            if result is None:
                raise UpstreamError(
                    f"MinerU 官方响应中没有文件 {filename!r} 的解析结果"
                )
            state = str(result.get("state") or "").lower()
            if state == "done":
                result_url = result.get("full_zip_url")
                if not isinstance(result_url, str) or not result_url:
                    raise UpstreamError(
                        "MinerU 官方任务已完成，但响应中没有 full_zip_url"
                    )
                return result_url
            if state == "failed":
                message = str(result.get("err_msg") or "未知错误")
                raise UpstreamError(f"MinerU 官方解析失败：{message}")
            if state not in _PENDING_STATES:
                raise UpstreamError(
                    f"MinerU 官方返回未知任务状态：{state or '空'}"
                )
            if time.monotonic() >= deadline:
                raise ServiceUnavailableError(
                    f"MinerU 解析等待超时（批次 {batch_id}），后台将继续重试"
                )
            await asyncio.sleep(self._poll_interval)

    async def _official_request(
        self, method: str, path: str, **kwargs: Any
    ) -> httpx.Response:
        url = f"{self._api_root}/{path.lstrip('/')}"
        await _validate_api_url(url)
        try:
            async with httpx.AsyncClient(timeout=self._request_timeout) as client:
                if method == "POST":
                    response = await client.post(
                        url, headers=self._headers, **kwargs
                    )
                else:
                    response = await client.request(
                        method, url, headers=self._headers, **kwargs
                    )
        except httpx.TimeoutException as exc:
            raise ServiceUnavailableError("MinerU 官方请求超时") from exc
        except httpx.RequestError as exc:
            raise ServiceUnavailableError(
                f"无法连接 MinerU 官方服务：{exc}"
            ) from exc
        return self._checked(response, "调用 MinerU 官方服务")


def _official_api_root(base_url: str) -> str:
    parsed = urlparse(base_url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ConfigurationError("MinerU 官方 Base URL 必须是安全的 HTTPS 地址")
    path = parsed.path.rstrip("/")
    if path in {"", "/api/v4"}:
        api_path = "/api/v4"
    elif path == "/api/v4/file-urls/batch":
        api_path = "/api/v4"
    else:
        raise ConfigurationError(
            "MinerU 官方 Base URL 路径必须为空、/api/v4 或 /api/v4/file-urls/batch"
        )
    return f"https://{parsed.netloc}{api_path}"


async def _validate_api_url(url: str) -> None:
    parsed = urlparse(url)
    host = parsed.hostname or ""
    port = parsed.port or 443
    await mineru_core._assert_public_host(host, port)


async def _validate_signed_url(url: str) -> None:
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise UpstreamError("MinerU 官方返回了不安全的签名上传地址")
    try:
        port = parsed.port or 443
    except ValueError as exc:
        raise UpstreamError("MinerU 官方返回了不安全的签名上传地址") from exc
    await mineru_core._assert_public_host(parsed.hostname, port)


async def _file_chunks(path: str) -> AsyncIterator[bytes]:
    with open(path, "rb") as source:
        while chunk := await asyncio.to_thread(source.read, 1024 * 1024):
            yield chunk


def _official_data(response: httpx.Response, action: str) -> dict[str, Any]:
    payload = mineru_core._response_payload(response)
    if not isinstance(payload, dict):
        raise UpstreamError(f"MinerU 官方{action}响应格式无效")
    code = payload.get("code")
    if str(code) != "0":
        message = mineru_core._find_error_message(payload) or f"错误码 {code}"
        trace_id = payload.get("trace_id")
        trace = f"（trace_id: {trace_id}）" if trace_id else ""
        raise UpstreamError(f"MinerU 官方{action}失败：{message}{trace}")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise UpstreamError(f"MinerU 官方{action}响应中没有 data")
    return data
