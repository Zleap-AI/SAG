from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest

from sag_api.fnos.open_api import FnOSOpenAPIClient, FnOSOpenAPIError

ResponseFactory = Callable[[dict[str, Any], str], Awaitable[tuple[int, object]]]


class FnOSOpenAPISimulator:
    def __init__(self, socket_path: Path, response_factory: ResponseFactory | None = None) -> None:
        self.socket_path = (
            socket_path
            if len(str(socket_path)) < 90
            else Path("/private/tmp") / f"sag-fnos-{uuid4().hex[:10]}.sock"
        )
        self.response_factory = response_factory or self._default_response
        self.requests: list[dict[str, Any]] = []
        self.bearers: list[str] = []
        self._server: asyncio.AbstractServer | None = None

    async def start(self) -> None:
        self._server = await asyncio.start_unix_server(self._handle, path=str(self.socket_path))

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
        self.socket_path.unlink(missing_ok=True)

    async def __aenter__(self) -> FnOSOpenAPISimulator:
        await self.start()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            header_blob = await reader.readuntil(b"\r\n\r\n")
            header_lines = header_blob.decode("latin-1").split("\r\n")
            request_line = header_lines[0]
            headers = {
                key.strip().lower(): value.strip()
                for line in header_lines[1:]
                if ":" in line
                for key, value in [line.split(":", 1)]
            }
            content_length = int(headers.get("content-length", "0"))
            request = json.loads((await reader.readexactly(content_length)).decode())
            request["_request_line"] = request_line
            bearer = headers.get("authorization", "")
            self.requests.append(request)
            self.bearers.append(bearer.removeprefix("Bearer "))
            status, response = await self.response_factory(request, bearer)
            if isinstance(response, bytes):
                body = response
            else:
                body = json.dumps(response).encode()
            reasons = {
                200: "OK",
                400: "Bad Request",
                401: "Unauthorized",
                403: "Forbidden",
                404: "Not Found",
                500: "Internal Server Error",
            }
            reason = reasons[status]
            response_headers = (
                f"HTTP/1.1 {status} {reason}\r\n"
                f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n"
                "Connection: close\r\n\r\n"
            )
            writer.write(
                response_headers.encode() + body
            )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    async def _default_response(self, request: dict[str, Any], _: str) -> tuple[int, object]:
        operation = request["req"]
        data: object
        if operation == "trim.system.getPlatformConfig":
            data = {"systemLanguage": "zh-CN", "systemVersion": "1.2.0500"}
        elif operation == "trim.file.getSharedAccessibleFolders":
            data = {"paths": ["/vol1/1000/docs", "/vol2/team"]}
        elif operation == "trim.file.checkUserACL":
            data = [
                {"path": path, "readable": True, "writable": False, "deletable": False}
                for path in request["data"]["path"]
            ]
        elif operation == "trim.file.convertPath":
            paths = request["data"]["path"]
            if isinstance(paths, str):
                paths = [paths]
            data = {
                "status": 0,
                "result": [{"path": path, "semanticPath": f"我的文件/{Path(path).name}"} for path in paths],
            }
        else:
            return 404, {"reqId": request["reqId"], "code": 200005, "msg": "Not Found", "data": {}}
        return 200, {"reqId": request["reqId"], "code": 0, "msg": "", "data": data}


@pytest.mark.asyncio
async def test_open_api_calls_real_temporary_uds_and_rereads_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with FnOSOpenAPISimulator(tmp_path / "host.sock") as server:
        client = FnOSOpenAPIClient(socket_path=server.socket_path)
        monkeypatch.setenv("TRIM_API_TOKEN", "first-token")
        config = await client.get_platform_config()
        monkeypatch.setenv("TRIM_API_TOKEN", "second-token")
        folders = await client.get_shared_accessible_folders()

    assert config.system_version == "1.2.0500"
    assert config.system_language == "zh-CN"
    assert [folder.path for folder in folders] == ["/vol1/1000/docs", "/vol2/team"]
    assert server.bearers == ["first-token", "second-token"]
    assert [request["_request_line"] for request in server.requests] == [
        "POST /api/v1/trimapp HTTP/1.1",
        "POST /api/v1/trimapp HTTP/1.1",
    ]
    assert server.requests[0]["appName"] == "sag"
    assert server.requests[0]["data"] == {}
    assert server.requests[0]["req"] == "trim.system.getPlatformConfig"
    assert server.requests[0]["reqId"] != server.requests[1]["reqId"]


@pytest.mark.asyncio
async def test_open_api_uses_official_acl_and_path_conversion_envelopes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TRIM_API_TOKEN", "test-token")
    async with FnOSOpenAPISimulator(tmp_path / "host.sock") as server:
        client = FnOSOpenAPIClient(socket_path=server.socket_path)
        acl = await client.check_user_acl(1000, ["/vol1/a.pdf", "/vol1/b.md"])
        converted = await client.convert_path(["/vol1/a.pdf"], "zh-CN")

    assert acl[0].path == "/vol1/a.pdf"
    assert acl[0].readable is True
    assert acl[0].writable is False
    assert acl[0].deletable is False
    assert converted[0].semantic_path == "我的文件/a.pdf"
    assert server.requests[0]["req"] == "trim.file.checkUserACL"
    assert server.requests[0]["data"] == {"uid": 1000, "path": ["/vol1/a.pdf", "/vol1/b.md"]}
    assert server.requests[1]["req"] == "trim.file.convertPath"
    assert server.requests[1]["data"] == {"path": ["/vol1/a.pdf"], "language": "zh-CN"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "fnos_code", "expected_code", "expected_status", "retryable"),
    [
        (401, 200004, "nas_host_auth_expired", 503, False),
        (403, 200003, "nas_scope_missing", 503, False),
        (404, 200005, "nas_host_api_not_found", 404, False),
        (400, 200001, "nas_host_request_rejected", 502, False),
        (500, 200006, "nas_host_api_unavailable", 503, True),
        (200, 200006, "nas_host_api_unavailable", 503, True),
    ],
)
async def test_open_api_maps_host_errors_to_stable_safe_codes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    fnos_code: int,
    expected_code: str,
    expected_status: int,
    retryable: bool,
) -> None:
    secret = "super-secret-token"
    monkeypatch.setenv("TRIM_API_TOKEN", secret)

    async def respond(request: dict[str, Any], _: str) -> tuple[int, object]:
        return status, {
            "reqId": request["reqId"],
            "code": fnos_code,
            "msg": f"host rejected {secret}",
            "data": {},
        }

    async with FnOSOpenAPISimulator(tmp_path / "host.sock", respond) as server:
        with pytest.raises(FnOSOpenAPIError) as captured:
            await FnOSOpenAPIClient(socket_path=server.socket_path).get_platform_config()

    error = captured.value
    assert error.code == expected_code
    assert error.status_code == expected_status
    assert error.retryable is retryable
    assert secret not in str(error)
    assert secret not in repr(error)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        b"not-json",
        {"reqId": "wrong-request", "code": 0, "msg": "", "data": {}},
        {"code": 0, "msg": "", "data": {}},
        {"reqId": "dynamic", "code": "0", "msg": "", "data": {}},
        {"reqId": "dynamic", "code": 0, "msg": 3, "data": {}},
    ],
)
async def test_open_api_rejects_malformed_or_mismatched_responses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, response: object
) -> None:
    monkeypatch.setenv("TRIM_API_TOKEN", "test-token")

    async def respond(request: dict[str, Any], _: str) -> tuple[int, object]:
        value = response
        if isinstance(value, dict) and value.get("reqId") == "dynamic":
            value = {**value, "reqId": request["reqId"]}
        return 200, value

    async with FnOSOpenAPISimulator(tmp_path / "host.sock", respond) as server:
        with pytest.raises(FnOSOpenAPIError, match="宿主响应格式无效") as captured:
            await FnOSOpenAPIClient(socket_path=server.socket_path).get_platform_config()

    assert captured.value.code == "nas_host_response_invalid"
    assert captured.value.status_code == 502


@pytest.mark.asyncio
async def test_open_api_rejects_invalid_typed_payload(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRIM_API_TOKEN", "test-token")

    async def respond(request: dict[str, Any], _: str) -> tuple[int, object]:
        return 200, {"reqId": request["reqId"], "code": 0, "msg": "", "data": {"paths": "not-a-list"}}

    async with FnOSOpenAPISimulator(tmp_path / "host.sock", respond) as server:
        with pytest.raises(FnOSOpenAPIError) as captured:
            await FnOSOpenAPIClient(socket_path=server.socket_path).get_shared_accessible_folders()

    assert captured.value.code == "nas_host_response_invalid"


@pytest.mark.asyncio
async def test_open_api_requires_token_without_disclosing_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TRIM_API_TOKEN", raising=False)
    with pytest.raises(FnOSOpenAPIError, match="宿主认证信息不可用") as captured:
        await FnOSOpenAPIClient(socket_path="/does/not/matter.sock").get_platform_config()

    assert captured.value.code == "nas_host_auth_expired"
    assert captured.value.status_code == 503
    assert captured.value.retryable is False


@pytest.mark.asyncio
async def test_open_api_maps_connection_and_timeout_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRIM_API_TOKEN", "test-token")
    client = FnOSOpenAPIClient(socket_path=tmp_path / "missing.sock", timeout_seconds=0.5)

    with pytest.raises(FnOSOpenAPIError, match="宿主文件服务暂时不可用") as captured:
        await client.get_platform_config()

    assert captured.value.code == "nas_host_api_unavailable"
    assert captured.value.status_code == 503
    assert captured.value.retryable is True


@pytest.mark.asyncio
async def test_open_api_maps_read_timeout_without_disclosing_token(monkeypatch: pytest.MonkeyPatch) -> None:
    secret = "timeout-secret-token"
    monkeypatch.setenv("TRIM_API_TOKEN", secret)

    async def raise_timeout(*_: object, **__: object) -> None:
        raise httpx.ReadTimeout(f"timed out with {secret}")

    monkeypatch.setattr(httpx.AsyncClient, "post", raise_timeout)
    with pytest.raises(FnOSOpenAPIError) as captured:
        await FnOSOpenAPIClient(socket_path="/unused.sock").get_platform_config()

    assert captured.value.code == "nas_host_api_unavailable"
    assert secret not in str(captured.value)
    assert secret not in repr(captured.value)
    assert captured.value.__cause__ is None


def test_open_api_settings_are_bounded() -> None:
    from pydantic import ValidationError

    from sag_api.core.config import Settings

    assert Settings().fnos_open_api_socket == "/var/run/trim_open_gateway_apiscope.socket"
    assert Settings().fnos_open_api_timeout_seconds == 5.0
    with pytest.raises(ValidationError):
        Settings(fnos_open_api_timeout_seconds=0.1)
    with pytest.raises(ValidationError):
        Settings(fnos_open_api_timeout_seconds=31)
