import base64
import hashlib
from pathlib import Path

import httpx
import pytest

from sag_api.client_access.allowlist import business_allowed, client_allowed
from sag_api.client_access.core import ClientAccessError
from sag_api.fnos.client_access import FnOSClientAccess
from sag_api.fnos.identity import GatewayIdentity


def seed_local_account(root, identity):
    import sqlite3

    from sag_api.fnos.workspace import WorkspacePaths

    paths = WorkspacePaths.for_identity(root, root / ".client-temp", identity)
    paths.prepare()
    with sqlite3.connect(paths.database_file) as db:
        db.execute(
            "CREATE TABLE users(id TEXT,created_at TEXT,auth_version INTEGER,"
            "password_hash TEXT,is_active INTEGER,name TEXT)"
        )
        db.execute(
            "INSERT INTO users VALUES (?, '2026-10-21',0,'hash',1,?)", (f"fnos_{identity.uid}", identity.username)
        )


def test_explicit_allowlist():
    assert business_allowed("POST", "/sources/a/documents/ingest")
    assert business_allowed("GET", "/sources/a/documents/b/read")
    assert not business_allowed("POST", "/system/model-config")
    assert not client_allowed("POST", "/api/v1/client-access/approve")
    assert not business_allowed("GET", "/sources/a%2fb")
    assert not business_allowed("GET", "/sources/a", b"/client/v1/api/sources/%2e%2e/a")


@pytest.mark.asyncio
async def test_host_account_lifecycle_fails_closed(tmp_path):
    access = FnOSClientAccess(tmp_path, b"x" * 32)
    identity = GatewayIdentity(1000, "alice", False)
    with pytest.raises(Exception, match="host_identity_unavailable"):
        await access.account(identity)


@pytest.mark.asyncio
async def test_host_approved_pair_exchange_recovery_and_reused_uid(tmp_path):
    generation = ["instance-a"]

    async def verify(identity):
        return generation[0]

    access = FnOSClientAccess(tmp_path, b"x" * 32, account_verifier=verify)
    identity = GatewayIdentity(1000, "alice", False)
    seed_local_account(tmp_path, identity)
    verifier = "a" * 43
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    item = access.pairings.create("CLI", challenge)
    await access.approve(identity, item["user_code"], True)
    ticket = access.pairings.poll(item["device_code"])["exchange_ticket"]
    pair = await access.exchange(ticket, verifier, "exchange-key")
    assert await access.route(pair["access_token"], "access") == identity
    assert await access.exchange(ticket, verifier, "exchange-key") == pair
    with pytest.raises(ClientAccessError):
        await access.exchange(ticket, verifier, "different-key")
    generation[0] = "instance-b"
    with pytest.raises(Exception, match="host_identity_changed"):
        await access.route(pair["access_token"], "access")


@pytest.mark.asyncio
async def test_default_account_identity_checks_existing_user_and_reset(tmp_path):
    import sqlite3

    from sag_api.fnos.workspace import WorkspacePaths

    access = FnOSClientAccess(tmp_path, b"x" * 32)
    identity = GatewayIdentity(1000, "alice", False)
    paths = WorkspacePaths.for_identity(tmp_path, tmp_path / ".client-temp", identity)
    paths.prepare()
    with sqlite3.connect(paths.database_file) as db:
        db.execute(
            "CREATE TABLE users(id TEXT,created_at TEXT,auth_version INTEGER,"
            "password_hash TEXT,is_active INTEGER,name TEXT)"
        )
        db.execute("INSERT INTO users VALUES ('fnos_1000','2026-10-21',0,'hash',1,'alice')")
    first = await access.account(identity)
    with sqlite3.connect(paths.database_file) as db:
        db.execute("UPDATE users SET auth_version=1")
    assert await access.account(identity) != first
    with sqlite3.connect(paths.database_file) as db:
        db.execute("UPDATE users SET is_active=0")
    with pytest.raises(Exception, match="host_identity_unavailable"):
        await access.account(identity)


@pytest.mark.asyncio
@pytest.mark.parametrize("external", [False, True])
async def test_real_tenant_worker_pairing_crud_revoke_and_cross_uid(tmp_path, monkeypatch, external):
    import os
    import shutil
    import tempfile

    from sag_api.fnos.gateway import create_gateway_app
    from sag_api.fnos.identity import InternalIdentitySigner, derive_fnos_internal_key
    from sag_api.fnos.supervisor import WorkerSupervisor

    runtime = Path(tempfile.mkdtemp(prefix="sag-ca-", dir=os.path.realpath("/tmp")))
    secret = runtime / "secret"
    secret.write_text("a" * 64)
    secret.chmod(0o600)
    monkeypatch.setenv("SAG_FNOS_INTERNAL_SECRET_FILE", str(secret))
    monkeypatch.setenv("SAG_FNOS_DATA_ROOT", str(runtime / "data"))
    monkeypatch.setenv("SAG_FNOS_TEMP_ROOT", str(runtime / "tmp"))
    signer = InternalIdentitySigner.from_file(secret)
    external_instance = ["host-account-instance-42"]

    async def verify_external(identity):
        return external_instance[0]

    access = FnOSClientAccess(
        runtime / "data",
        derive_fnos_internal_key(secret, b"sag-client-access-v1"),
        account_verifier=verify_external if external else None,
    )
    admin = GatewayIdentity(1000, "Alice", True)
    access.update_policy(
        admin, {"enabled": True, "api_url": "http://nas:15167", "verification_uri": "http://nas/app/sag/settings"}
    )
    supervisor = WorkerSupervisor(runtime / "data", runtime / "tmp", identity_signer=signer)
    app = create_gateway_app(supervisor, signer, "http://127.0.0.1:3091", client_access=access)
    web = {"X-Trim-Userid": "1000", "X-Trim-Username": "Alice", "X-Trim-Isadmin": "true"}
    try:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gateway") as client:
                # Real worker authentication initializes the actual SAG account.
                assert (await client.get("/app/sag/api/v1/sources", headers=web)).status_code == 200
                verifier = "a" * 43
                challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
                item = (
                    await client.post(
                        "/app/sag/client/v1/device/code",
                        json={"client_name": "CLI", "code_challenge": challenge, "code_challenge_method": "S256"},
                    )
                ).json()
                assert (
                    await client.post(
                        "/app/sag/api/v1/client-access/approve", json={"user_code": item["user_code"], "approve": True}
                    )
                ).status_code == 401
                assert (
                    await client.post(
                        "/app/sag/api/v1/client-access/approve",
                        headers=web,
                        json={"user_code": item["user_code"], "approve": True},
                    )
                ).status_code == 204
                poll = (
                    await client.post(
                        "/app/sag/client/v1/token",
                        json={
                            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                            "device_code": item["device_code"],
                        },
                    )
                ).json()
                pair = (
                    await client.post(
                        "/app/sag/client/v1/token",
                        headers={"Idempotency-Key": "exchange-one"},
                        json={
                            "grant_type": "urn:sag:params:oauth:grant-type:device-ticket",
                            "exchange_ticket": poll["exchange_ticket"],
                            "code_verifier": verifier,
                        },
                    )
                ).json()
                headers = {"Authorization": "Bearer " + pair["access_token"], "Idempotency-Key": "source-one"}
                created = await client.post(
                    "/app/sag/client/v1/api/sources", headers=headers, json={"name": "automated"}
                )
                assert created.status_code == 201, created.text
                assert (
                    await client.post("/app/sag/client/v1/api/sources", headers=headers, json={"name": "automated"})
                ).json() == created.json()
                source_id = created.json()["id"]
                assert (await client.get("/app/sag/client/v1/api/sources", headers=headers)).json()[0][
                    "id"
                ] == source_id
                if external:
                    for value in (None, "changed-host-account"):
                        external_instance[0] = value
                        assert (await client.get("/app/sag/client/v1/api/sources", headers=headers)).status_code in (
                            401,
                            503,
                        )
                    external_instance[0] = "host-account-instance-42"
                    import sqlite3

                    from sag_api.fnos.workspace import WorkspacePaths

                    database = WorkspacePaths.for_identity(runtime / "data", runtime / "tmp", admin).database_file
                    for change, restore in (
                        ("is_active=0", "is_active=1"),
                        ("auth_version=auth_version+1", "auth_version=auth_version-1"),
                        ("id='removed'", "id='fnos_1000'"),
                    ):
                        with sqlite3.connect(database) as db:
                            db.execute("UPDATE users SET " + change)
                        assert (await client.get("/app/sag/client/v1/api/sources", headers=headers)).status_code in (
                            401,
                            503,
                        )
                        with sqlite3.connect(database) as db:
                            db.execute("UPDATE users SET " + restore)
                    assert (await client.get("/app/sag/client/v1/api/sources", headers=headers)).status_code == 200
                other = {**web, "X-Trim-Userid": "1001", "X-Trim-Username": "Bob"}
                assert (await client.get(f"/app/sag/api/v1/sources/{source_id}", headers=other)).status_code == 404
                assert (
                    await client.get(
                        "/app/sag/client/v1/api/sources", headers={"Authorization": "Bearer " + pair["refresh_token"]}
                    )
                ).status_code == 401
                operation = await client.get("/app/sag/client/v1/operations/source-one", headers=headers)
                assert operation.json()["response"] == created.json()
                assert (
                    await client.delete("/app/sag/api/v1/client-access/sessions/" + pair["session_id"], headers=other)
                ).status_code == 404
                assert (
                    await client.delete("/app/sag/api/v1/client-access/sessions/" + pair["session_id"], headers=web)
                ).status_code == 204
                assert (await client.get("/app/sag/client/v1/api/sources", headers=headers)).status_code == 401
    finally:
        shutil.rmtree(runtime, ignore_errors=True)


def test_cold_backup_preserves_client_session_policy_and_key(tmp_path, monkeypatch):
    import importlib.util
    import sqlite3
    import tarfile

    from sag_api.client_access.core import SessionStore

    runtime = Path(__file__).resolve().parents[3] / "packages/fnos/native/sag/app/runtime/lifecycle.py"
    spec = importlib.util.spec_from_file_location("client_lifecycle", runtime)
    lifecycle = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(lifecycle)
    root = tmp_path / "pkgvar"
    meta = root / "users/1000/meta"
    meta.mkdir(parents=True)
    monkeypatch.setenv("TRIM_PKGVAR", str(root))
    with sqlite3.connect(meta / "sag.db") as db:
        db.execute("CREATE TABLE users(id TEXT)")
    (root / "internal-secret").write_text("a" * 64)
    (root / "client-access-policy.json").write_text('{"server_id":"server","enabled":true}')
    store = SessionStore(meta / "client-access.sqlite", b"x" * 32, "server")
    pair = store.issue("fnos_1000", "CLI", "exchange-one")
    archive = root / "backup/cold.tar.gz.tmp"
    lifecycle.backup(root / "users", archive)
    lifecycle.validate(archive)
    restored = tmp_path / "restored"
    with tarfile.open(archive) as data:
        data.extractall(restored, filter="data")
    assert (restored / "internal-secret").read_text() == "a" * 64
    assert (restored / "client-access-policy.json").read_text() == (root / "client-access-policy.json").read_text()
    recovered = SessionStore(restored / "users/1000/meta/client-access.sqlite", b"x" * 32, "server")
    assert recovered.authenticate(pair["access_token"])["subject"] == "fnos_1000"


@pytest.mark.asyncio
async def test_completed_exchange_recovers_after_gateway_restart(tmp_path):
    async def verify(identity):
        return "account-one"

    first = FnOSClientAccess(tmp_path, b"x" * 32, account_verifier=verify)
    seed_local_account(tmp_path, GatewayIdentity(1000, "Alice", False))
    verifier = "a" * 43
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    item = first.pairings.create("CLI", challenge)
    await first.approve(GatewayIdentity(1000, "Alice", False), item["user_code"], True)
    ticket = first.pairings.poll(item["device_code"])["exchange_ticket"]
    pair = await first.exchange(ticket, verifier, "exchange-one")
    restarted = FnOSClientAccess(tmp_path, b"x" * 32, account_verifier=verify)
    assert await restarted.exchange(ticket, verifier, "exchange-one") == pair
    with pytest.raises(ClientAccessError):
        await restarted.exchange(ticket, verifier, "different-key")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "condition", ["recover", "recovery_expired", "revoked", "account_changed", "superseded", "wrong_key", "no_recovery"]
)
async def test_token_route_recovers_only_existing_rotation_after_old_refresh_expiry(tmp_path, monkeypatch, condition):
    import sqlite3

    from fastapi import FastAPI

    from sag_api.client_access import core
    from sag_api.fnos.client_routes import install_client_routes
    from sag_api.fnos.workspace import WorkspacePaths

    identity = GatewayIdentity(1000, "Alice", True)
    seed_local_account(tmp_path, identity)
    access = FnOSClientAccess(tmp_path, b"x" * 32)
    access.update_policy(identity, {"enabled": True})
    route = {"uid": 1000, "username": "Alice", "account_instance": await access.account(identity)}
    store = access.store(identity)
    original = store.issue("fnos_1000", "CLI", "initial-exchange", now=0, claims=route)
    clock = [90 * core.DAY - 1]
    monkeypatch.setattr(core, "_now", lambda value: clock[0] if value is None else value)
    app = FastAPI()
    install_client_routes(app, access, None, None, "")
    body = {"grant_type": "refresh_token", "refresh_token": original["refresh_token"]}
    headers = {"Idempotency-Key": "rotation-one"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gateway") as client:
        if condition != "no_recovery":
            response = await client.post("/client/v1/token", json=body, headers=headers)
            assert response.status_code == 200
            pair = response.json()
        clock[0] += 2
        if condition == "recovery_expired":
            clock[0] += 300
        elif condition == "revoked":
            store.revoke(pair["access_token"])
        elif condition == "account_changed":
            database = WorkspacePaths.for_identity(tmp_path, tmp_path / ".client-temp", identity).database_file
            with sqlite3.connect(database) as db:
                db.execute("UPDATE users SET auth_version=1")
        elif condition == "superseded":
            store.rotate(pair["refresh_token"], "rotation-two")
        elif condition == "wrong_key":
            headers = {"Idempotency-Key": "rotation-other"}
        recovered = await client.post("/client/v1/token", json=body, headers=headers)
        if condition == "recover":
            assert recovered.status_code == 200, recovered.text
            assert recovered.json() == pair
        else:
            assert recovered.status_code in (401, 409), recovered.text
