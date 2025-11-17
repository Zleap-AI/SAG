"""SAG API 主入口

FastAPI 应用入口，配置路由、中间件、全局异常处理
"""

from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from sag import __version__
from sag.api.middleware import LoggingMiddleware, TimingMiddleware
from sag.api.routers import (
    chat,
    documents,
    entity_types,
    model_configs,
    pipeline,
    sources,
    tasks,
)
from sag.api.schemas.common import ErrorResponse
from sag.core.config.settings import get_settings
from sag.exceptions import SAGError


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator:
    """应用生命周期管理"""
    # 启动时初始化
    print("🚀 SAG API 启动...")
    print(f"📦 版本: {__version__}")

    settings = get_settings()
    print(f"🔧 配置加载完成")
    print(f"   - Database: {settings.mysql_host}:{settings.mysql_port}")
    print(f"   - Elasticsearch: {settings.elasticsearch_url}")
    print(f"   - Redis: {settings.redis_host}:{settings.redis_port}")

    yield

    # 关闭时清理
    print("👋 SAG API 关闭...")


# 创建 FastAPI 应用
app = FastAPI(
    title="SAG API",
    description="基于 SQL-RAG 理论实现的数据流智能引擎 - 为 Web UI 提供后端支持",
    version=__version__,
    lifespan=lifespan,
    docs_url="/api/docs",
    redoc_url="/api/redoc",
    openapi_url="/api/openapi.json",
)

# CORS 配置
settings = get_settings()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 开发环境允许所有来源，生产环境需要配置具体域名
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 添加自定义中间件
app.add_middleware(TimingMiddleware)
app.add_middleware(LoggingMiddleware)


# 全局异常处理
@app.exception_handler(SAGError)
async def sag_exception_handler(request: Request, exc: SAGError):
    """SAG 业务异常处理"""
    return JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST,
        content=ErrorResponse(
            success=False,
            error={
                "code": exc.__class__.__name__,
                "message": str(exc),
                "details": getattr(exc, "details", None),
            },
        ).model_dump(),
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """参数验证异常处理"""
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content=ErrorResponse(
            success=False,
            error={
                "code": "VALIDATION_ERROR",
                "message": "请求参数验证失败",
                "details": exc.errors(),
            },
        ).model_dump(),
    )


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """全局异常处理"""
    print(f"❌ 未处理的异常: {exc}")
    import traceback

    traceback.print_exc()

    # 获取配置以决定是否显示详细错误
    try:
        settings = get_settings()
        show_details = getattr(settings, "debug", False)
    except:
        show_details = False

    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content=ErrorResponse(
            success=False,
            error={
                "code": "INTERNAL_ERROR",
                "message": "服务器内部错误",
                "details": str(exc) if show_details else None,
            },
        ).model_dump(),
    )


# 注册路由
app.include_router(sources.router, prefix="/api/v1", tags=["信息源管理"])
app.include_router(entity_types.router, prefix="/api/v1", tags=["实体维度管理"])
app.include_router(documents.router, prefix="/api/v1", tags=["文档管理"])
app.include_router(pipeline.router, prefix="/api/v1", tags=["统一流程"])
app.include_router(tasks.router, prefix="/api/v1", tags=["任务管理"])
app.include_router(chat.router, prefix="/api/v1", tags=["AI对话"])
# 模型配置路由
app.include_router(model_configs.router, prefix="/api/v1/model-configs", tags=["模型配置"])


# 健康检查
@app.get("/health", tags=["系统"])
async def health_check():
    """健康检查"""
    return {
        "status": "healthy",
        "version": __version__,
        "service": "SAG API",
    }


# 首页
@app.get("/", tags=["系统"])
async def root():
    """API 首页"""
    return {
        "service": "SAG API",
        "version": __version__,
        "description": "基于 SQL-RAG 理论实现的数据流智能引擎",
        "docs": "/api/docs",
        "redoc": "/api/redoc",
        "health": "/health",
        "features": {
            "source_management": "信息源配置管理",
            "custom_entity_types": "自定义实体维度",
            "document_upload": "文档上传处理",
            "load_extract_search": "Load-Extract-Search 流程",
            "flexible_combination": "灵活组合，可分可合",
        },
    }


if __name__ == "__main__":
    import os
    import uvicorn

    # 从环境变量读取配置
    workers = int(os.getenv("API_WORKERS", "1"))
    host = os.getenv("API_HOST", "0.0.0.0")
    port = int(os.getenv("API_PORT", "8000"))
    
    print(f"🔧 Uvicorn 配置:")
    print(f"   Host: {host}")
    print(f"   Port: {port}")
    print(f"   Workers: {workers}")
    
    if workers > 1:
        # 多 worker 模式（生产环境 - Linux 服务器）
        print(f"🚀 启动生产模式 ({workers} workers)...")
        uvicorn.run(
            "sag.api.main:app",
            host=host,
            port=port,
            workers=workers,
            log_level="info",
        )
    else:
        # 单 worker 模式（开发环境 - 本地/macOS）
        print(f"🚀 启动开发模式 (单 worker, 热重载)...")
        uvicorn.run(
            "sag.api.main:app",
            host=host,
            port=port,
            reload=True,  # 开发模式支持热重载
            log_level="info",
        )

