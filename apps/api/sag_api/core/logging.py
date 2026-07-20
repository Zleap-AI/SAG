"""轻量日志配置 + 请求追踪中间件。"""

from __future__ import annotations

import contextvars
import logging
import sys
import uuid

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.types import ASGIApp

_CONFIGURED = False

# 当前请求的追踪 id，供日志与错误处理引用
request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")


def configure_logging(level: str = "INFO") -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(_RequestIdFilter())
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s  %(levelname)-7s  [%(request_id)s]  %(name)s  %(message)s",
            datefmt="%H:%M:%S",
        )
    )
    root = logging.getLogger()
    root.setLevel(level)
    root.handlers = [handler]
    # 降低第三方噪音，并禁止模型客户端在 DEBUG 模式输出完整提示词/正文。
    for noisy in (
        "httpx",
        "httpcore",
        "openai",
        "lancedb",
        "aiosqlite",
        "LiteLLM",
        "LiteLLM Router",
        "LiteLLM Proxy",
    ):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    logging.getLogger("zleap.sag.ai.openai").setLevel(logging.INFO)
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"sag.{name}")


class _RequestIdFilter(logging.Filter):
    """把当前请求 id 注入每条日志记录，未在请求上下文时为 '-'。"""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        return True


class RequestContextMiddleware(BaseHTTPMiddleware):
    """为每个请求分配追踪 id：入站取 X-Request-Id 或新生成，出站回写响应头。"""

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(self, request: Request, call_next):
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex[:16]
        token = request_id_var.set(rid)
        request.state.request_id = rid
        try:
            response = await call_next(request)
        finally:
            request_id_var.reset(token)
        response.headers["X-Request-Id"] = rid
        return response
