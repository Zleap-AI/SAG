"""Desktop sidecar entry point."""

from __future__ import annotations

import multiprocessing
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
    # PyInstaller children re-enter this executable. Dispatch multiprocessing
    # bootstrap arguments before Uvicorn starts, otherwise an OCTX worker would
    # launch a second API server and collide with the desktop sidecar port.
    multiprocessing.freeze_support()
    control_token = os.environ.pop("SAG_DESKTOP_CONTROL_TOKEN", None)
    if control_token:
        from sag_api.desktop_control import DesktopControl, has_background_work
        from sag_api.desktop_process import contain_windows_children

        contain_windows_children()
        from sag_api.main import app

        def shutdown() -> None:
            server.should_exit = True

        controlled_app = DesktopControl(app, control_token, lambda: has_background_work(app), shutdown)
        server = uvicorn.Server(
            uvicorn.Config(
                controlled_app,
                host=os.getenv("SAG_DESKTOP_HOST", "127.0.0.1"),
                port=_port(),
                log_level="info",
                access_log=False,
                timeout_graceful_shutdown=5,
            )
        )
        server.run()
        return
    uvicorn.run(
        "sag_api.main:app",
        host=os.getenv("SAG_DESKTOP_HOST", "127.0.0.1"),
        port=_port(),
        log_level="info",
        access_log=False,
    )


if __name__ == "__main__":
    main()
