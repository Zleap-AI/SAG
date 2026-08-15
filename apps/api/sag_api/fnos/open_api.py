"""Typed client for fnOS host Open APIs exposed over the private Unix socket."""

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn
from uuid import uuid4

import httpx

from sag_api.core.config import get_settings
from sag_api.core.error_taxonomy import ErrorCode, ErrorLayer, ErrorStage
from sag_api.core.errors import ApiError

OPEN_API_OPERATIONS = {
    "platform": "trim.system.getPlatformConfig",
    "shared_folders": "trim.file.getSharedAccessibleFolders",
    "user_acl": "trim.file.checkUserACL",
    "convert_path": "trim.file.convertPath",
}


@dataclass(frozen=True)
class PlatformConfig:
    system_version: str
    system_language: str | None = None


@dataclass(frozen=True)
class SharedFolder:
    path: str


@dataclass(frozen=True)
class UserACL:
    path: str
    readable: bool
    writable: bool
    deletable: bool


@dataclass(frozen=True)
class ConvertedPath:
    path: str
    semantic_path: str


class FnOSOpenAPIError(ApiError):
    """A safe, stable failure returned by the fnOS host boundary."""

    layer = ErrorLayer.API

    def __init__(
        self,
        message: str,
        *,
        code: ErrorCode,
        status_code: int,
        retryable: bool,
        stage: ErrorStage = ErrorStage.UNKNOWN,
    ) -> None:
        self.status_code = status_code
        super().__init__(message, code=code, stage=stage, retryable=retryable)


class FnOSOpenAPIClient:
    """Call the fnOS host without ever caching or exposing its bearer token."""

    def __init__(
        self,
        *,
        socket_path: str | Path | None = None,
        timeout_seconds: float | None = None,
    ) -> None:
        settings = get_settings()
        self._socket_path = str(socket_path or settings.fnos_open_api_socket)
        self._timeout_seconds = timeout_seconds or settings.fnos_open_api_timeout_seconds

    async def get_platform_config(self) -> PlatformConfig:
        data = await self._call(OPEN_API_OPERATIONS["platform"], {})
        payload = _require_dict(data)
        system_version = _require_string(payload.get("systemVersion"))
        system_language_value = payload.get("systemLanguage")
        if system_language_value is not None and not isinstance(system_language_value, str):
            _raise_invalid_response()
        return PlatformConfig(system_version=system_version, system_language=system_language_value)

    async def get_shared_accessible_folders(self) -> list[SharedFolder]:
        data = _require_dict(await self._call(OPEN_API_OPERATIONS["shared_folders"], {}))
        paths = data.get("paths")
        if not isinstance(paths, list) or not all(isinstance(path, str) and path for path in paths):
            _raise_invalid_response()
        return [SharedFolder(path=path) for path in paths]

    async def check_user_acl(self, uid: int, paths: Sequence[str]) -> list[UserACL]:
        path_list = list(paths)
        data = await self._call(OPEN_API_OPERATIONS["user_acl"], {"uid": uid, "path": path_list})
        if not isinstance(data, list):
            _raise_invalid_response()
        result: list[UserACL] = []
        for item in data:
            payload = _require_dict(item)
            path = _require_string(payload.get("path"))
            readable = _require_bool(payload.get("readable"))
            writable = _require_bool(payload.get("writable"))
            deletable = _require_bool(payload.get("deletable"))
            result.append(UserACL(path=path, readable=readable, writable=writable, deletable=deletable))
        return result

    async def convert_path(self, path: str | Sequence[str], language: str) -> list[ConvertedPath]:
        request_path: str | list[str] = path if isinstance(path, str) else list(path)
        data = _require_dict(
            await self._call(
                OPEN_API_OPERATIONS["convert_path"],
                {"path": request_path, "language": language},
            )
        )
        if type(data.get("status")) is not int or data["status"] != 0:
            _raise_invalid_response()
        items = data.get("result")
        if not isinstance(items, list):
            _raise_invalid_response()
        result: list[ConvertedPath] = []
        for item in items:
            payload = _require_dict(item)
            result.append(
                ConvertedPath(
                    path=_require_string(payload.get("path")),
                    semantic_path=_require_string(payload.get("semanticPath")),
                )
            )
        return result

    async def _call(self, operation: str, parameters: dict[str, Any]) -> object:
        token = os.environ.get("TRIM_API_TOKEN", "").strip()
        if not token:
            raise FnOSOpenAPIError(
                "宿主认证信息不可用",
                code=ErrorCode.NAS_HOST_AUTH_EXPIRED,
                status_code=503,
                retryable=False,
                stage=ErrorStage.AUTH,
            )

        request_id = uuid4().hex
        request = {
            "reqId": request_id,
            "req": operation,
            "appName": "sag",
            "data": parameters,
        }
        transport = httpx.AsyncHTTPTransport(uds=self._socket_path)
        try:
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://localhost",
                timeout=httpx.Timeout(self._timeout_seconds),
            ) as client:
                response = await client.post(
                    "/api/v1/trimapp",
                    json=request,
                    headers={"Authorization": f"Bearer {token}"},
                )
        except (httpx.TimeoutException, httpx.NetworkError):
            raise FnOSOpenAPIError(
                "宿主文件服务暂时不可用",
                code=ErrorCode.NAS_HOST_API_UNAVAILABLE,
                status_code=503,
                retryable=True,
            ) from None

        try:
            payload = response.json()
        except ValueError:
            _raise_invalid_response()
        if not isinstance(payload, dict):
            _raise_invalid_response()
        response_request_id = payload.get("reqId")
        response_code = payload.get("code")
        response_message = payload.get("msg")
        if (
            response_request_id != request_id
            or type(response_code) is not int
            or not isinstance(response_message, str)
        ):
            _raise_invalid_response()
        if response.status_code >= 400 or response_code != 0:
            _raise_host_error(response.status_code, response_code)
        if "data" not in payload:
            _raise_invalid_response()
        return payload["data"]


def _raise_host_error(status_code: int, host_code: int) -> NoReturn:
    if status_code == 401 or host_code == 200004:
        raise FnOSOpenAPIError(
            "宿主认证信息已失效",
            code=ErrorCode.NAS_HOST_AUTH_EXPIRED,
            status_code=503,
            retryable=False,
            stage=ErrorStage.AUTH,
        )
    if status_code == 403 or host_code == 200003:
        raise FnOSOpenAPIError(
            "应用缺少宿主文件访问权限",
            code=ErrorCode.NAS_SCOPE_MISSING,
            status_code=503,
            retryable=False,
            stage=ErrorStage.AUTH,
        )
    if status_code == 404 or host_code == 200005:
        raise FnOSOpenAPIError(
            "当前系统未提供所需的宿主文件接口",
            code=ErrorCode.NAS_HOST_API_NOT_FOUND,
            status_code=404,
            retryable=False,
        )
    if host_code == 200001:
        raise FnOSOpenAPIError(
            "宿主拒绝了文件访问请求",
            code=ErrorCode.NAS_HOST_REQUEST_REJECTED,
            status_code=502,
            retryable=False,
        )
    if status_code >= 500 or host_code == 200006:
        raise FnOSOpenAPIError(
            "宿主文件服务暂时不可用",
            code=ErrorCode.NAS_HOST_API_UNAVAILABLE,
            status_code=503,
            retryable=True,
        )
    _raise_invalid_response()


def _require_dict(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        _raise_invalid_response()
    return value


def _require_string(value: object) -> str:
    if not isinstance(value, str) or not value:
        _raise_invalid_response()
    return value


def _require_bool(value: object) -> bool:
    if type(value) is not bool:
        _raise_invalid_response()
    return value


def _raise_invalid_response() -> NoReturn:
    raise FnOSOpenAPIError(
        "宿主响应格式无效",
        code=ErrorCode.NAS_HOST_RESPONSE_INVALID,
        status_code=502,
        retryable=False,
    )
