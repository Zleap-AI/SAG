"""Subprocess-level proof that fnOS identities never share a SAG workspace."""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

import httpx
import pytest

from sag_api.fnos.gateway import create_gateway_app
from sag_api.fnos.identity import InternalIdentitySigner
from sag_api.fnos.supervisor import WorkerSupervisor

FNOS_A = {"X-Trim-Userid": "1000", "X-Trim-Username": "Alice", "X-Trim-Isadmin": "false"}
FNOS_B = {"X-Trim-Userid": "1001", "X-Trim-Username": "Alice", "X-Trim-Isadmin": "false"}
FNOS_A_RENAMED = {"X-Trim-Userid": "1000", "X-Trim-Username": "Alicia", "X-Trim-Isadmin": "false"}
KEY_A = "1000-Alice-3bc51062"
KEY_B = "1001-Alice-3bc51062"
KEY_A_RENAMED = "1000-Alicia-2a4f079d"


def _short_tmp_root() -> str:
    """Portable short temp root — /tmp on Linux CI, /private/tmp on macOS via symlink.

    Return the realpath so callers hand the workspace validator a canonical path
    (macOS /tmp → /private/tmp) and never trip UnsafeWorkspacePath.
    """
    for candidate in ("/tmp", "/private/tmp"):
        if os.path.isdir(candidate):
            return os.path.realpath(candidate)
    return os.path.realpath(tempfile.gettempdir())


@pytest.mark.asyncio
async def test_two_fnos_users_receive_disjoint_worker_databases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
    """The complete Gateway → UDS Worker path must preserve UID isolation.

    With username isolation off (the default) the layout is pure-UID.
    """
    monkeypatch.delenv("SAG_FNOS_USERNAME_ISOLATION", raising=False)
    secret = tmp_path / "internal-secret"
    secret.write_text("a" * 64, encoding="ascii")
    secret.chmod(0o600)
    monkeypatch.setenv("SAG_FNOS_INTERNAL_SECRET_FILE", str(secret))
    monkeypatch.setenv("SAG_FNOS_DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setenv("SAG_FNOS_TEMP_ROOT", str(tmp_path / "tmp"))
    monkeypatch.setenv("SAG_LLM_API_KEY", "")
    monkeypatch.setenv("SAG_EMBEDDING_API_KEY", "")
    # Unix socket names have a kernel path-length limit; production fnOS paths
    # are short, while pytest's standard temporary root isn't.
    runtime_root = Path(tempfile.mkdtemp(prefix="sag-fnos-", dir=_short_tmp_root()))
    request.addfinalizer(lambda: shutil.rmtree(runtime_root, ignore_errors=True))
    (runtime_root / "data").mkdir()
    (runtime_root / "tmp").mkdir()
    signer = InternalIdentitySigner.from_file(secret)
    supervisor = WorkerSupervisor(runtime_root / "data", runtime_root / "tmp", identity_signer=signer)
    app = create_gateway_app(supervisor, signer, "http://127.0.0.1:3091")
    transport = httpx.ASGITransport(app=app)

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://gateway") as client:
            created = await client.post("/app/sag/api/v1/sources", headers=FNOS_A, json={"name": "private"})
            assert created.status_code == 201, created.text
            source_id = created.json()["id"]
            own = await client.get("/app/sag/api/v1/sources", headers=FNOS_A)
            other = await client.get("/app/sag/api/v1/sources", headers=FNOS_B)
            guessed = await client.get(f"/app/sag/api/v1/sources/{source_id}", headers=FNOS_B)

    assert [source["id"] for source in own.json()] == [source_id]
    assert other.json() == []
    assert guessed.status_code == 404
    assert (runtime_root / "data" / "users" / "1000" / "meta" / "sag.db").is_file()
    assert (runtime_root / "data" / "users" / "1001" / "meta" / "sag.db").is_file()
    assert os.path.realpath(runtime_root / "data" / "users" / "1000" / "meta" / "sag.db") != os.path.realpath(
        runtime_root / "data" / "users" / "1001" / "meta" / "sag.db"
    )


@pytest.mark.asyncio
async def test_username_isolation_gives_renamed_and_uid_reused_accounts_empty_workspaces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
    """With the isolation switch on, the Gateway → UDS Worker path must key tenants by
    uid+username: a renamed (or UID-reused) account starts from an empty workspace while
    the old directory stays on disk."""
    monkeypatch.setenv("SAG_FNOS_USERNAME_ISOLATION", "1")
    secret = tmp_path / "internal-secret"
    secret.write_text("a" * 64, encoding="ascii")
    secret.chmod(0o600)
    monkeypatch.setenv("SAG_FNOS_INTERNAL_SECRET_FILE", str(secret))
    monkeypatch.setenv("SAG_FNOS_DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setenv("SAG_FNOS_TEMP_ROOT", str(tmp_path / "tmp"))
    monkeypatch.setenv("SAG_LLM_API_KEY", "")
    monkeypatch.setenv("SAG_EMBEDDING_API_KEY", "")
    # Unix socket names have a kernel path-length limit; production fnOS paths
    # are short, while pytest's standard temporary root isn't.
    runtime_root = Path(tempfile.mkdtemp(prefix="sag-fnos-", dir=_short_tmp_root()))
    request.addfinalizer(lambda: shutil.rmtree(runtime_root, ignore_errors=True))
    (runtime_root / "data").mkdir()
    (runtime_root / "tmp").mkdir()
    signer = InternalIdentitySigner.from_file(secret)
    supervisor = WorkerSupervisor(runtime_root / "data", runtime_root / "tmp", identity_signer=signer)
    app = create_gateway_app(supervisor, signer, "http://127.0.0.1:3091")
    transport = httpx.ASGITransport(app=app)

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://gateway") as client:
            created = await client.post("/app/sag/api/v1/sources", headers=FNOS_A, json={"name": "private"})
            assert created.status_code == 201, created.text
            source_id = created.json()["id"]
            own = await client.get("/app/sag/api/v1/sources", headers=FNOS_A)
            other = await client.get("/app/sag/api/v1/sources", headers=FNOS_B)
            guessed = await client.get(f"/app/sag/api/v1/sources/{source_id}", headers=FNOS_B)
            renamed = await client.get("/app/sag/api/v1/sources", headers=FNOS_A_RENAMED)

    assert [source["id"] for source in own.json()] == [source_id]
    assert other.json() == []
    assert guessed.status_code == 404
    assert renamed.status_code == 200
    assert renamed.json() == []
    for key in (KEY_A, KEY_B, KEY_A_RENAMED):
        assert (runtime_root / "data" / "users" / key / "meta" / "sag.db").is_file()
    assert os.path.realpath(runtime_root / "data" / "users" / KEY_A / "meta" / "sag.db") != os.path.realpath(
        runtime_root / "data" / "users" / KEY_B / "meta" / "sag.db"
    )

