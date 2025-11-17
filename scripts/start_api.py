"""启动 SAG API 服务

生产环境启动脚本
"""

import uvicorn

from sag.core.config.settings import get_settings


def main():
    """启动 API"""
    settings = get_settings()

    # 从环境变量获取配置，或使用默认值
    host = getattr(settings, "api_host", "0.0.0.0")
    port = getattr(settings, "api_port", 8000)
    workers = getattr(settings, "api_workers", 4)
    debug = getattr(settings, "debug", False)

    print(f"🚀 启动 SAG API 服务")
    print(f"   主机: {host}")
    print(f"   端口: {port}")
    print(f"   Worker数: {workers if not debug else 1}")
    print(f"   调试模式: {debug}")

    uvicorn.run(
        "sag.api.main:app",
        host=host,
        port=port,
        workers=workers if not debug else 1,
        reload=debug,
        log_level="info" if not debug else "debug",
    )


if __name__ == "__main__":
    main()

