"""sag-api 应用入口。"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from sag_api import __version__
from sag_api.api.v1 import api_router
from sag_api.branding import PRODUCT_NAME
from sag_api.core.config import settings
from sag_api.core.db import SessionLocal, dispose_db, init_db
from sag_api.core.error_taxonomy import ErrorCode, ErrorLayer, ErrorStage
from sag_api.core.errors import ApiError
from sag_api.core.logging import RequestContextMiddleware, configure_logging, get_logger

# [storage-bootstrap] 唯一允许的 upgrades 包入口；删除 sag_api/upgrades/ 时还原本行
from sag_api.upgrades.integration import bind_storage_bootstrap, install_storage_bootstrap_middleware

log = get_logger("app")


# 已知不安全的默认密钥（生产环境拒绝启动）
_INSECURE_SECRETS = {
    "dev-insecure-secret-change-me-in-production-0123456789",
    "please-change-this-in-production-0123456789",
    "dev-secret-change-me",
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging("DEBUG" if settings.debug else "INFO")
    if settings.environment == "prod" and settings.secret_key in _INSECURE_SECRETS:
        raise RuntimeError(
            "生产环境禁止使用默认 SAG_SECRET_KEY。请设置强随机值（≥32 字节），例如：openssl rand -hex 32"
        )
    os.makedirs(settings.data_dir, exist_ok=True)
    os.makedirs(settings.upload_dir, exist_ok=True)

    await init_db()

    # 把 DB 里保存的模型配置覆盖到 settings 单例（在构建 LLM/引擎之前）
    from sag_api.services.settings_service import apply_startup_overrides

    await apply_startup_overrides(SessionLocal)

    try:
        from sag_api.services.dsh_integration_service import write_connection_file

        async with SessionLocal() as session:
            await write_connection_file(session)
    except OSError as error:
        log.warning("DSH 本机连接文件刷新失败：%s", error)

    # [storage-bootstrap] 引导用户迁移存量数据；删除 sag_api/upgrades/ 时连同下方 finally 中标注的两行一起还原
    storage_bootstrap = bind_storage_bootstrap(app, settings, SessionLocal)
    storage_status = await storage_bootstrap.inspect()

    try:
        if storage_status.runtime_ready:
            await storage_bootstrap.install_runtime()
        else:
            log.info("存储引导等待用户选择 phase=%s", storage_status.phase.value)
        yield
    finally:
        failures: list[BaseException] = []
        for cleanup in (
            storage_bootstrap.wait,  # [storage-bootstrap]
            storage_bootstrap.stop_runtime,  # [storage-bootstrap]
            dispose_db,
        ):
            try:
                await cleanup()
            except BaseException as error:
                failures.append(error)
        if failures:
            raise BaseExceptionGroup("application shutdown failed", failures)


def create_app() -> FastAPI:
    app = FastAPI(
        title=f"{PRODUCT_NAME} API",
        version=__version__,
        summary="开源知识库平台 · 从信息源到知识问答",
        lifespan=lifespan,
    )

    cors_kwargs: dict = {
        "allow_origins": settings.cors_origins,
        "allow_credentials": True,
        "allow_methods": ["*"],
        "allow_headers": ["*"],
        "expose_headers": ["X-Request-Id"],
    }
    # 开发环境放行局域网前端（如 http://192.168.x.x:3000），避免本机 IP 访问时 CORS 拦截
    if settings.environment == "dev":
        cors_kwargs["allow_origin_regex"] = (
            r"https?://("
            r"localhost|"
            r"127\.0\.0\.1|"
            r"192\.168\.\d{1,3}\.\d{1,3}|"
            r"10\.\d{1,3}\.\d{1,3}\.\d{1,3}|"
            r"172\.(1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}"
            r")(:\d+)?"
        )
    app.add_middleware(CORSMiddleware, **cors_kwargs)
    # [storage-bootstrap] 存储引导期间拦截未就绪请求（删除 sag_api/upgrades/ 时还原本行）
    install_storage_bootstrap_middleware(app)
    # 请求追踪（放在 CORS 之后添加 → 更外层执行，最先分配 request_id）
    app.add_middleware(RequestContextMiddleware)

    @app.exception_handler(ApiError)
    async def _handle_api_error(request: Request, exc: ApiError) -> JSONResponse:
        request_id = getattr(request.state, "request_id", None)
        return JSONResponse(
            status_code=exc.status_code,
            content=exc.to_envelope(request_id=request_id),
        )

    @app.exception_handler(Exception)
    async def _handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        log.exception("未处理异常：%s", exc)
        request_id = getattr(request.state, "request_id", None)
        error: dict[str, object] = {
            "code": ErrorCode.INTERNAL_ERROR,
            "message": "服务器内部错误",
            "layer": ErrorLayer.API.value,
            "stage": ErrorStage.UNKNOWN.value,
            "retryable": False,
        }
        if request_id:
            error["request_id"] = request_id
        return JSONResponse(status_code=500, content={"error": error})

    app.include_router(api_router)

    # 信源即 MCP：挂载 Streamable-HTTP 端点（失败不阻断应用启动）
    try:
        from sag_api.mcp.mount import attach_source_mcp

        app.state.source_mcp = attach_source_mcp(app)
    except Exception as e:  # noqa: BLE001
        app.state.source_mcp = None
        log.warning("MCP 端点挂载失败：%s", e)

    @app.get("/", tags=["system"])
    async def root() -> dict:
        return {"name": PRODUCT_NAME, "version": __version__, "docs": "/docs"}

    return app


app = create_app()
