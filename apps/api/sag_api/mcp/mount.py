"""把 SAG 知识库 MCP 作为 Streamable-HTTP 端点挂进 FastAPI。

外部宿主（Claude Desktop / Cursor）可挂载：

    http://<host>/mcp/                         # 整个知识库
    http://<host>/mcp/?source_id=<信源 id>     # 单个信源

请求先校验 JWT，再根据可选的 `source_id` 载入一个或全部 Source 并注入 contextvar。
作用域随请求隔离，外部宿主与进程内 agent 可共用同一 server。
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from urllib.parse import parse_qs

import jwt
from mcp.server.transport_security import TransportSecuritySettings
from sqlalchemy import select

from sag_api.core.db import SessionLocal
from sag_api.core.logging import get_logger
from sag_api.core.security import decode_token
from sag_api.db.models import Source
from sag_api.mcp.server import build_source_mcp, use_scope

if TYPE_CHECKING:
    from fastapi import FastAPI
    from mcp.server.fastmcp import FastMCP

log = get_logger("mcp.http")


async def _send_json(send, status: int, payload: dict) -> None:
    body = json.dumps(payload).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [(b"content-type", b"application/json")],
        }
    )
    await send({"type": "http.response.body", "body": body})


def _bearer(scope) -> str | None:
    for name, value in scope.get("headers") or []:
        if name == b"authorization":
            raw = value.decode("latin-1")
            return raw[7:].strip() if raw.lower().startswith("bearer ") else raw.strip()
    return None


class ScopedKnowledgeMCP:
    """ASGI 包装：鉴权并注入全库或单信源作用域，再委托给 MCP 应用。"""

    def __init__(self, parent_app: FastAPI, mcp_asgi) -> None:
        self._parent = parent_app
        self._mcp = mcp_asgi

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self._mcp(scope, receive, send)
            return

        params = parse_qs((scope.get("query_string") or b"").decode())
        source_id = (params.get("source_id") or [""])[0].strip()

        token = _bearer(scope)
        if not token:
            await _send_json(send, 401, {"error": "缺少认证令牌"})
            return
        try:
            decode_token(token)
        except jwt.PyJWTError:
            await _send_json(send, 401, {"error": "令牌无效或已过期"})
            return

        async with SessionLocal() as session:
            statement = select(Source).order_by(Source.created_at, Source.id)
            if source_id:
                statement = statement.where(Source.id == source_id)
            sources = tuple((await session.execute(statement)).scalars().all())
        if source_id and not sources:
            await _send_json(send, 404, {"error": "信源不存在"})
            return

        engine_manager = self._parent.state.engine_manager
        with use_scope(engine_manager, sources):
            await self._mcp(scope, receive, send)


def attach_source_mcp(app: FastAPI) -> FastMCP:
    """构造 HTTP 版知识库 MCP 并挂到 `/mcp`。

    内层 FastMCP 的路由改到根 `/`，外层用 `Mount("/mcp")` 承接——避免 `/mcp` 内再套 `/mcp`
    的双重路径。外部宿主使用带斜杠的 `/mcp/`；`source_id` 仅用于可选的单源兼容模式。
    """
    # FastMCP 默认把 host=127.0.0.1 解释为“只接受 localhost Host”。
    # 该 ASGI 应用实际挂在可通过局域网/反向代理访问的 FastAPI 下，并在外层强制
    # Bearer 鉴权，因此关闭 SDK 的 localhost 专用 Host 白名单。
    mcp = build_source_mcp(
        stateless_http=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    mcp.settings.streamable_http_path = "/"
    mcp_asgi = mcp.streamable_http_app()  # 惰性创建 session_manager
    app.mount("/mcp", ScopedKnowledgeMCP(app, mcp_asgi))
    return mcp
