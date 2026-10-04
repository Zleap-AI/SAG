"""Exercise real LiteLLM and zleap boundaries in isolated processes, with controlled HTTP."""

import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize("case", ["generation", "extraction", "schema", "retry", "agent"])
@pytest.mark.parametrize("model", ["vendor/exact-model", "qwen3.6-flash", "deepseek-v4-pro"])
def test_native_paths(case, model):
    args = [sys.executable, str(Path(__file__).with_name("probe.py")), case]
    if model != "vendor/exact-model":
        args += [model]
    result = subprocess.run(args, env=os.environ.copy(), capture_output=True, text=True, timeout=45)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("case", ["generation", "extraction"])
def test_unsupported_reasoning_is_not_retried_or_downgraded(case):
    result = subprocess.run(
        [sys.executable, str(Path(__file__).with_name("probe.py")), case, "qwen3.6-flash", "unsupported"],
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        timeout=45,
    )
    assert result.returncode == 0, result.stdout + result.stderr
