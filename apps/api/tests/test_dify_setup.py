"""可选 Dify 初始化命令的密钥落盘行为。"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_SCRIPT = _PROJECT_ROOT / "scripts" / "setup_dify.py"


def _run_setup(env_file: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(_SCRIPT),
            "--env-file",
            str(env_file),
        ],
        check=False,
        capture_output=True,
        text=True,
    )


def _read_key(env_file: Path) -> str:
    for line in env_file.read_text(encoding="utf-8").splitlines():
        if line.startswith("SAG_DIFY_API_KEY="):
            return line.partition("=")[2]
    raise AssertionError("SAG_DIFY_API_KEY was not written")


def test_setup_dify_creates_an_idempotent_env_file(tmp_path):
    env_file = tmp_path / ".env"

    first = _run_setup(env_file)
    assert first.returncode == 0, first.stderr
    first_key = _read_key(env_file)
    assert len(first_key) >= 32
    assert f"API Key: {first_key}" in first.stdout
    assert "Endpoint: http://sag:8000/api/v1/dify" in first.stdout
    if os.name != "nt":
        assert stat.S_IMODE(env_file.stat().st_mode) == 0o600

    original = env_file.read_text(encoding="utf-8")
    second = _run_setup(env_file)
    assert second.returncode == 0, second.stderr
    assert env_file.read_text(encoding="utf-8") == original
    assert f"API Key: {first_key}" in second.stdout


def test_setup_dify_fills_empty_key_and_preserves_existing_configuration(
    tmp_path,
):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "WEB_PORT=3100\nSAG_DIFY_API_KEY=\nSAG_LLM_MODEL=test-model\n",
        encoding="utf-8",
    )

    filled = _run_setup(env_file)
    assert filled.returncode == 0, filled.stderr
    generated_key = _read_key(env_file)
    assert generated_key
    assert "WEB_PORT=3100\n" in env_file.read_text(encoding="utf-8")
    assert "SAG_LLM_MODEL=test-model\n" in env_file.read_text(encoding="utf-8")

    env_file.write_text(
        "WEB_PORT=3100\nSAG_DIFY_API_KEY=keep-this-key\n",
        encoding="utf-8",
    )
    preserved = _run_setup(env_file)
    assert preserved.returncode == 0, preserved.stderr
    assert _read_key(env_file) == "keep-this-key"
    assert "API Key: keep-this-key" in preserved.stdout
