from __future__ import annotations

import importlib
import os
import sys
import types
from pathlib import Path

import pytest

KEY_ADA = "1000-Ada-99a563ab"
# Key for the normalized empty username: sha256("")[:8] == e3b0c442.
KEY_EMPTY = "1000--e3b0c442"


def test_worker_sets_uid_scoped_settings_before_importing_application(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
    """Catches a child worker inheriting its parent process database configuration."""
    original_environment = os.environ.copy()
    request.addfinalizer(lambda: (os.environ.clear(), os.environ.update(original_environment)))
    monkeypatch.setenv("SAG_DATABASE_URL", "sqlite+aiosqlite:////parent/sag.db")
    monkeypatch.setenv("SAG_FNOS_DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setenv("SAG_FNOS_TEMP_ROOT", str(tmp_path / "tmp"))
    # Worker configuration mutates these names before importing the app; register
    # their original state with monkeypatch so this unit test cannot affect the
    # following in-process API tests.
    for name in (
        "SAG_AUTH_MODE",
        "SAG_FNOS_UID",
        "SAG_DATA_DIR",
        "SAG_UPLOAD_DIR",
        "SAG_ENGINE_CACHE_SIZE",
        "SAG_ENGINE_WARMUP_COUNT",
        "SAG_FNOS_USERNAME",
        "SAG_FNOS_USERNAME_ISOLATION",
    ):
        monkeypatch.delenv(name, raising=False)

    captured: dict[str, object] = {}
    uvicorn = importlib.import_module("uvicorn")
    monkeypatch.setattr(
        uvicorn,
        "run",
        lambda app, **kwargs: captured.update(app=app, **kwargs),
    )
    captured_environment: dict[str, str] = {}
    application = types.ModuleType("sag_api.main")

    def create_app():
        for name in (
            "SAG_DATABASE_URL",
            "SAG_DATA_DIR",
            "SAG_UPLOAD_DIR",
            "SAG_AUTH_MODE",
            "SAG_FNOS_UID",
            "SAG_FNOS_USERNAME",
        ):
            captured_environment[name] = __import__("os").environ[name]
        return object()

    application.create_app = create_app
    monkeypatch.setitem(sys.modules, "sag_api.main", application)

    worker = importlib.import_module("sag_api.fnos.worker")
    worker.main(
        [
            "--uid",
            "1000",
            "--username",
            "Ada",
            "--socket",
            str(tmp_path / "tmp/workers/1000.sock"),
        ]
    )

    assert captured_environment["SAG_DATABASE_URL"] == f"sqlite+aiosqlite:////{tmp_path}/data/users/1000/meta/sag.db"
    assert captured_environment["SAG_DATA_DIR"] == str(tmp_path / "data/users/1000/engine")
    assert captured_environment["SAG_UPLOAD_DIR"] == str(tmp_path / "data/users/1000/uploads")
    assert captured_environment["SAG_AUTH_MODE"] == "fnos"
    assert captured_environment["SAG_FNOS_UID"] == "1000"
    assert captured_environment["SAG_FNOS_USERNAME"] == "Ada"
    assert captured["uds"] == str(tmp_path / "tmp/workers/1000.sock")
    assert captured["workers"] == 1
    assert captured["proxy_headers"] is False


def test_worker_uses_tenant_key_paths_when_isolation_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
    """Catches the worker deriving pure-UID paths while the gateway derives composite ones."""
    original_environment = os.environ.copy()
    request.addfinalizer(lambda: (os.environ.clear(), os.environ.update(original_environment)))
    monkeypatch.setenv("SAG_DATABASE_URL", "sqlite+aiosqlite:////parent/sag.db")
    monkeypatch.setenv("SAG_FNOS_DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setenv("SAG_FNOS_TEMP_ROOT", str(tmp_path / "tmp"))
    for name in (
        "SAG_AUTH_MODE",
        "SAG_FNOS_UID",
        "SAG_DATA_DIR",
        "SAG_UPLOAD_DIR",
        "SAG_ENGINE_CACHE_SIZE",
        "SAG_ENGINE_WARMUP_COUNT",
        "SAG_FNOS_USERNAME",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SAG_FNOS_USERNAME_ISOLATION", "1")

    captured: dict[str, object] = {}
    uvicorn = importlib.import_module("uvicorn")
    monkeypatch.setattr(
        uvicorn,
        "run",
        lambda app, **kwargs: captured.update(app=app, **kwargs),
    )
    captured_environment: dict[str, str] = {}
    application = types.ModuleType("sag_api.main")

    def create_app():
        for name in (
            "SAG_DATABASE_URL",
            "SAG_DATA_DIR",
            "SAG_UPLOAD_DIR",
            "SAG_AUTH_MODE",
            "SAG_FNOS_UID",
            "SAG_FNOS_USERNAME",
        ):
            captured_environment[name] = __import__("os").environ[name]
        return object()

    application.create_app = create_app
    monkeypatch.setitem(sys.modules, "sag_api.main", application)

    worker = importlib.import_module("sag_api.fnos.worker")
    worker.main(
        ["--uid", "1000", "--username", "Ada", "--socket", str(tmp_path / f"tmp/workers/{KEY_ADA}.sock")]
    )

    assert (
        captured_environment["SAG_DATABASE_URL"]
        == f"sqlite+aiosqlite:////{tmp_path}/data/users/{KEY_ADA}/meta/sag.db"
    )
    assert captured_environment["SAG_DATA_DIR"] == str(tmp_path / f"data/users/{KEY_ADA}/engine")
    assert captured_environment["SAG_UPLOAD_DIR"] == str(tmp_path / f"data/users/{KEY_ADA}/uploads")
    assert captured_environment["SAG_FNOS_USERNAME"] == "Ada"
    assert captured["uds"] == str(tmp_path / f"tmp/workers/{KEY_ADA}.sock")


@pytest.mark.parametrize("username", ["", "a" * 121])
def test_worker_normalizes_its_username_before_deriving_the_tenant_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest, username: str
) -> None:
    """Catches the worker skipping normalization and diverging from the supervisor's key.

    An over-long gateway username normalizes to "" on the supervisor side. A worker
    that kept the raw value would derive a different tenant key, fail the socket
    equality guard, and leave that user unable to ever start a worker.
    """
    original_environment = os.environ.copy()
    request.addfinalizer(lambda: (os.environ.clear(), os.environ.update(original_environment)))
    monkeypatch.setenv("SAG_DATABASE_URL", "sqlite+aiosqlite:////parent/sag.db")
    monkeypatch.setenv("SAG_FNOS_DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setenv("SAG_FNOS_TEMP_ROOT", str(tmp_path / "tmp"))
    for name in (
        "SAG_AUTH_MODE",
        "SAG_FNOS_UID",
        "SAG_DATA_DIR",
        "SAG_UPLOAD_DIR",
        "SAG_ENGINE_CACHE_SIZE",
        "SAG_ENGINE_WARMUP_COUNT",
        "SAG_FNOS_USERNAME",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SAG_FNOS_USERNAME_ISOLATION", "1")

    captured: dict[str, object] = {}
    uvicorn = importlib.import_module("uvicorn")
    monkeypatch.setattr(
        uvicorn,
        "run",
        lambda app, **kwargs: captured.update(app=app, **kwargs),
    )
    captured_environment: dict[str, str] = {}
    application = types.ModuleType("sag_api.main")

    def create_app():
        for name in (
            "SAG_DATABASE_URL",
            "SAG_DATA_DIR",
            "SAG_UPLOAD_DIR",
            "SAG_AUTH_MODE",
            "SAG_FNOS_UID",
            "SAG_FNOS_USERNAME",
        ):
            captured_environment[name] = __import__("os").environ[name]
        return object()

    application.create_app = create_app
    monkeypatch.setitem(sys.modules, "sag_api.main", application)

    worker = importlib.import_module("sag_api.fnos.worker")
    worker.main(
        ["--uid", "1000", "--username", username, "--socket", str(tmp_path / f"tmp/workers/{KEY_EMPTY}.sock")]
    )

    assert captured_environment["SAG_FNOS_USERNAME"] == ""
    assert (
        captured_environment["SAG_DATABASE_URL"]
        == f"sqlite+aiosqlite:////{tmp_path}/data/users/{KEY_EMPTY}/meta/sag.db"
    )
    assert captured_environment["SAG_DATA_DIR"] == str(tmp_path / f"data/users/{KEY_EMPTY}/engine")
    assert captured_environment["SAG_UPLOAD_DIR"] == str(tmp_path / f"data/users/{KEY_EMPTY}/uploads")
    assert captured["uds"] == str(tmp_path / f"tmp/workers/{KEY_EMPTY}.sock")


def test_worker_rejects_a_socket_outside_its_uid_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches a caller selecting another user's worker socket path."""
    monkeypatch.delenv("SAG_FNOS_USERNAME_ISOLATION", raising=False)
    monkeypatch.setenv("SAG_FNOS_DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setenv("SAG_FNOS_TEMP_ROOT", str(tmp_path / "tmp"))
    worker = importlib.import_module("sag_api.fnos.worker")

    with pytest.raises(SystemExit):
        worker.main(
            [
                "--uid",
                "1000",
                "--username",
                "Ada",
                "--socket",
                str(tmp_path / "tmp/workers/1001.sock"),
            ]
        )


@pytest.mark.parametrize(
    "socket_name",
    ["1001-Ada-99a563ab.sock", "1000-Bob-cd9fb1e1.sock", "1000.sock"],
)
def test_worker_rejects_a_socket_outside_its_tenant_scope_when_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, socket_name: str
) -> None:
    """Catches a caller binding this worker to another tenant's socket path."""
    monkeypatch.setenv("SAG_FNOS_USERNAME_ISOLATION", "1")
    monkeypatch.setenv("SAG_FNOS_DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setenv("SAG_FNOS_TEMP_ROOT", str(tmp_path / "tmp"))
    worker = importlib.import_module("sag_api.fnos.worker")

    with pytest.raises(SystemExit):
        worker.main(
            ["--uid", "1000", "--username", "Ada", "--socket", str(tmp_path / "tmp/workers" / socket_name)]
        )
