"""Per-user fnOS UDS worker entry point."""

from __future__ import annotations

import argparse
import os
from collections.abc import Sequence
from pathlib import Path

from sag_api.fnos.identity import GatewayIdentity, normalize_username
from sag_api.fnos.workspace import WorkspacePaths


def _root_from_environment(name: str) -> Path:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} must be set for the fnOS worker")
    path = Path(value)
    if not path.is_absolute():
        raise RuntimeError(f"{name} must be an absolute path")
    return path


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run one UID-scoped fnOS SAG worker")
    parser.add_argument("--uid", type=int, required=True)
    parser.add_argument("--username", required=True)
    parser.add_argument("--socket", required=True)
    return parser.parse_args(argv)


def _configure_environment(paths: WorkspacePaths, uid: int, username: str) -> None:
    os.environ["SAG_AUTH_MODE"] = "fnos"
    os.environ["SAG_FNOS_UID"] = str(uid)
    os.environ["SAG_FNOS_USERNAME"] = username
    os.environ["SAG_DATABASE_URL"] = f"sqlite+aiosqlite:////{paths.database_file}"
    os.environ["SAG_DATA_DIR"] = str(paths.engine_dir)
    os.environ["SAG_UPLOAD_DIR"] = str(paths.uploads_dir)
    os.environ["SAG_ENGINE_CACHE_SIZE"] = "2"
    os.environ["SAG_ENGINE_WARMUP_COUNT"] = "1"


def main(argv: Sequence[str] | None = None) -> None:
    args = _parse_args(argv)
    username = normalize_username(args.username)
    paths = WorkspacePaths.for_identity(
        _root_from_environment("SAG_FNOS_DATA_ROOT"),
        _root_from_environment("SAG_FNOS_TEMP_ROOT"),
        GatewayIdentity(uid=args.uid, username=username, is_admin=False),
    )
    socket_file = Path(args.socket)
    if not socket_file.is_absolute() or socket_file != paths.socket_file:
        raise SystemExit("worker socket must be the tenant-scoped fnOS worker socket")

    paths.prepare()
    _configure_environment(paths, args.uid, username)

    # Settings is a process singleton, so the application import must remain below
    # the user-specific environment setup above.
    import uvicorn

    from sag_api.main import create_app

    uvicorn.run(
        create_app(),
        uds=str(paths.socket_file),
        workers=1,
        proxy_headers=False,
    )


if __name__ == "__main__":
    main()
