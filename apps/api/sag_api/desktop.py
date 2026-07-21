"""Desktop sidecar entry point."""

from __future__ import annotations

import os

import uvicorn


def _port() -> int:
    value = os.getenv("SAG_DESKTOP_PORT", "8000")
    try:
        port = int(value)
    except ValueError as error:
        raise RuntimeError("SAG_DESKTOP_PORT must be an integer") from error
    if not 1 <= port <= 65535:
        raise RuntimeError("SAG_DESKTOP_PORT must be between 1 and 65535")
    return port


def main() -> None:
    uvicorn.run(
        "sag_api.main:app",
        host=os.getenv("SAG_DESKTOP_HOST", "127.0.0.1"),
        port=_port(),
        log_level="info",
        access_log=False,
    )


if __name__ == "__main__":
    main()
