#!/usr/bin/env python3
"""Generate and persist the optional Dify integration key."""

from __future__ import annotations

import argparse
import os
import secrets
from pathlib import Path

_SETTING_NAME = "SAG_DIFY_API_KEY"
_DIFY_ENDPOINT = "http://sag:8000/api/v1/dify"


def _secure_env_file(env_file: Path) -> None:
    if os.name != "nt":
        env_file.chmod(0o600)


def configure_dify_key(env_file: Path) -> str:
    text = env_file.read_text(encoding="utf-8") if env_file.exists() else ""
    lines = text.splitlines(keepends=True)

    for index, line in enumerate(lines):
        body = line.rstrip("\r\n")
        ending = line[len(body) :]
        name, separator, value = body.partition("=")
        if separator and name.strip() == _SETTING_NAME:
            existing = value.strip()
            if existing:
                _secure_env_file(env_file)
                return existing
            key = secrets.token_urlsafe(32)
            lines[index] = f"{_SETTING_NAME}={key}{ending}"
            env_file.parent.mkdir(parents=True, exist_ok=True)
            env_file.write_text("".join(lines), encoding="utf-8")
            _secure_env_file(env_file)
            return key

    key = secrets.token_urlsafe(32)
    prefix = text
    if prefix and not prefix.endswith(("\n", "\r")):
        prefix += "\n"
    env_file.parent.mkdir(parents=True, exist_ok=True)
    env_file.write_text(
        f"{prefix}{_SETTING_NAME}={key}\n",
        encoding="utf-8",
    )
    _secure_env_file(env_file)
    return key


def main() -> int:
    project_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(
        description="Configure the opt-in SAG Dify external knowledge API key."
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=project_root / ".env",
        help="Path to the SAG .env file (default: project root .env).",
    )
    args = parser.parse_args()

    key = configure_dify_key(args.env_file)
    print("Dify external knowledge integration is configured.")
    print(f"Endpoint: {_DIFY_ENDPOINT}")
    print(f"API Key: {key}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
