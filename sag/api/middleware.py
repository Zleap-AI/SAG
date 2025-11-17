"""中间件

提供请求日志、性能监控等功能
"""

import time
from typing import Callable

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware


class TimingMiddleware(BaseHTTPMiddleware):
    """性能计时中间件"""

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        start_time = time.time()
        response = await call_next(request)
        duration = time.time() - start_time

        response.headers["X-Process-Time"] = str(duration)
        return response


class LoggingMiddleware(BaseHTTPMiddleware):
    """请求日志中间件"""

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        # 记录请求
        print(f"📥 {request.method} {request.url.path}")

        response = await call_next(request)

        # 记录响应
        print(f"📤 {request.method} {request.url.path} - {response.status_code}")

        return response

