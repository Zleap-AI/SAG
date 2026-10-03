"""The shipped engine artifact must be intact and match the installed runtime."""

import base64
import csv
import hashlib
import importlib.metadata
import io
import tomllib
from pathlib import Path
from zipfile import ZipFile

API_ROOT = Path(__file__).resolve().parents[1]


def _locked_engine():
    lock = tomllib.loads((API_ROOT / "uv.lock").read_text(encoding="utf-8"))
    return next(package for package in lock["package"] if package["name"] == "zleap-sag")


def test_packaged_engine_hotfix_matches_runtime_and_lock():
    package = _locked_engine()
    assert package["version"] == importlib.metadata.version("zleap-sag") == "0.13.0+sag.1"
    wheel = API_ROOT / package["source"]["path"]
    assert "sha256:" + hashlib.sha256(wheel.read_bytes()).hexdigest() == package["wheels"][0]["hash"]


def test_hotfix_wheel_record_and_upstream_license_are_preserved():
    wheel = API_ROOT / _locked_engine()["source"]["path"]
    with ZipFile(wheel) as archive:
        record = next(name for name in archive.namelist() if name.endswith(".dist-info/RECORD"))
        rows = csv.reader(io.StringIO(archive.read(record).decode("utf-8")))
        for name, digest, size in rows:
            if name == record:
                assert digest == size == ""
                continue
            content = archive.read(name)
            assert len(content) == int(size)
            expected = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b"=").decode("ascii")
            assert digest == f"sha256={expected}"
        license_path = next(name for name in archive.namelist() if name.endswith("/licenses/LICENSE"))
        license_text = archive.read(license_path).decode("utf-8")
        assert "MIT License" in license_text
        assert "Copyright (c) 2026 Zleap Team" in license_text
