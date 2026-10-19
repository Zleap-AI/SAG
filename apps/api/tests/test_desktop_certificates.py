"""Exercise the certificate hook selected by the actual desktop packaging spec."""

from __future__ import annotations

import importlib.util
import json
import os
import runpy
import subprocess
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import certifi
import pytest


@pytest.fixture
def desktop_analysis(monkeypatch):
    """Evaluate the production spec without collecting or freezing dependencies."""
    hooks = ModuleType("PyInstaller.utils.hooks")
    hooks.collect_all = lambda *_args, **_kwargs: ([], [], [])
    hooks.collect_data_files = lambda package: [(f"{package}/data", package)]
    hooks.collect_submodules = lambda *_args: []
    hooks.copy_metadata = lambda *_args, **_kwargs: []
    for name in ("PyInstaller", "PyInstaller.utils"):
        monkeypatch.setitem(sys.modules, name, ModuleType(name))
    monkeypatch.setitem(sys.modules, hooks.__name__, hooks)
    captured = {}

    def analysis(*_args, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(pure=[], scripts=[], binaries=[], datas=kwargs["datas"])

    spec = Path(__file__).parents[1] / "packaging" / "sag-api.spec"
    runpy.run_path(str(spec), init_globals={
        "SPECPATH": str(spec.parent),
        "Analysis": analysis,
        **{name: lambda *_args, **_kwargs: None for name in ("PYZ", "EXE", "COLLECT")},
    })
    return captured


def _run_hooks(analysis):
    for hook in analysis["runtime_hooks"]:
        runpy.run_path(str(hook))


def test_packaged_default_uses_bundled_ca_without_changing_other_overrides(desktop_analysis, monkeypatch):
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", "requests-private-ca.pem")
    monkeypatch.setenv("CURL_CA_BUNDLE", "curl-private-ca.pem")

    _run_hooks(desktop_analysis)

    selected_ca = os.environ.get("SSL_CERT_FILE")
    assert selected_ca == certifi.where()
    assert "SSL_CERT_DIR" not in os.environ
    assert os.environ["REQUESTS_CA_BUNDLE"] == "requests-private-ca.pem"
    assert os.environ["CURL_CA_BUNDLE"] == "curl-private-ca.pem"
    assert ("certifi/data", "certifi") in desktop_analysis["datas"]


@pytest.mark.parametrize("overrides", [
    {"SSL_CERT_FILE": "private CA/自定义.pem"},
    {"SSL_CERT_DIR": "private CA/根证书"},
    {"SSL_CERT_FILE": "missing.pem", "SSL_CERT_DIR": "missing-directory"},
    {"SSL_CERT_FILE": ""},
    {"SSL_CERT_DIR": ""},
])
def test_explicit_certificate_configuration_is_preserved(desktop_analysis, monkeypatch, overrides):
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)
    for key, value in overrides.items():
        monkeypatch.setenv(key, value)
    before = dict(os.environ)

    _run_hooks(desktop_analysis)

    unchanged = dict(os.environ) == before
    assert unchanged, "the desktop hook changed explicit certificate configuration"


def test_fresh_aiohttp_import_receives_verified_roots_before_caching_context(desktop_analysis):
    """Model a clean desktop with no OpenSSL installation at the build-machine path."""
    environment = os.environ.copy()
    environment.pop("SSL_CERT_FILE", None)
    environment.pop("SSL_CERT_DIR", None)
    program = """
import json
import os
import runpy
import ssl
import sys

def load_portable_defaults(context, purpose=ssl.Purpose.SERVER_AUTH):
    # The packaged interpreter's compiled-in trust file does not exist on the
    # customer's machine. Only the portable environment-selected file exists.
    if os.environ.get("SSL_CERT_FILE"):
        context.load_verify_locations(cafile=os.environ["SSL_CERT_FILE"])

ssl.SSLContext.load_default_certs = load_portable_defaults
for hook in json.loads(sys.argv[1]):
    runpy.run_path(hook)
from aiohttp.connector import _SSL_CONTEXT_VERIFIED as context
assert context.cert_store_stats()["x509_ca"] > 0, "aiohttp cached an empty trust store"
assert context.verify_mode == ssl.CERT_REQUIRED
assert context.check_hostname is True
"""
    result = subprocess.run(
        [sys.executable, "-c", program, json.dumps(desktop_analysis["runtime_hooks"])],
        env=environment, text=True, capture_output=True, timeout=30, check=False,
    )

    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(importlib.util.find_spec("PyInstaller") is None, reason="requires the desktop build extra")
def test_frozen_aiohttp_uses_portable_ca_and_preserves_custom_trust(desktop_analysis, tmp_path):
    """Freeze a tiny consumer with the production hooks, then execute the native binary."""
    script = tmp_path / "certificate_probe.py"
    script.write_text('''
import json
import os
import ssl
import sys
from pathlib import Path
import certifi
from aiohttp.connector import _SSL_CONTEXT_VERIFIED as context

Path(sys.argv[1]).write_text(json.dumps({
    "selected_file": os.environ.get("SSL_CERT_FILE"),
    "selected_directory": os.environ.get("SSL_CERT_DIR"),
    "bundled_ca": certifi.where(),
    "bundle_root": sys._MEIPASS,
    "roots": context.cert_store_stats()["x509_ca"],
    "verified": context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname,
}), encoding="utf-8")
''', encoding="utf-8")
    command = [
        sys.executable, "-m", "PyInstaller", "--onedir", "--noconfirm", "--clean",
        "--log-level", "WARN", "--distpath", str(tmp_path / "dist"),
        "--workpath", str(tmp_path / "build"), "--specpath", str(tmp_path),
        "--collect-data", "certifi",
    ]
    for hook in desktop_analysis["runtime_hooks"]:
        command.extend(["--runtime-hook", str(hook)])
    if sys.platform == "win32":
        command.append("--noconsole")
    built = subprocess.run(command + [str(script)], text=True, capture_output=True, timeout=240, check=False)
    assert built.returncode == 0, built.stderr[-6000:]
    name = "certificate_probe.exe" if sys.platform == "win32" else "certificate_probe"
    executable = tmp_path / "dist" / "certificate_probe" / name
    environment = os.environ.copy()
    environment.pop("SSL_CERT_FILE", None)
    environment.pop("SSL_CERT_DIR", None)

    def probe(overrides):
        output = tmp_path / "result.json"
        output.unlink(missing_ok=True)
        completed = subprocess.run(
            [str(executable), str(output)], env=environment | overrides,
            text=True, capture_output=True, timeout=30, check=False,
        )
        assert completed.returncode == 0, completed.stderr
        return json.loads(output.read_text(encoding="utf-8"))

    default = probe({})
    assert default["selected_file"] == default["bundled_ca"]
    assert Path(default["bundled_ca"]).is_file()
    assert Path(default["bundled_ca"]).is_relative_to(default["bundle_root"])
    assert default["roots"] > 0
    assert default["verified"] is True
    custom_file = tmp_path / "private CA 根证书.pem"
    custom_file.write_bytes(Path(default["bundled_ca"]).read_bytes())
    custom_directory = tmp_path / "private CA 目录"
    custom_directory.mkdir()
    for overrides in (
        {"SSL_CERT_FILE": str(custom_file)},
        {"SSL_CERT_DIR": str(custom_directory)},
        {"SSL_CERT_FILE": ""},
    ):
        configured = probe(overrides)
        assert configured["selected_file"] == overrides.get("SSL_CERT_FILE")
        assert configured["selected_directory"] == overrides.get("SSL_CERT_DIR")
        assert configured["verified"] is True
