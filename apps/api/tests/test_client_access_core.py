import base64
import hashlib

import pytest

from sag_api.client_access.core import ClientAccessError, SessionStore
from sag_api.client_access.pairing import PairingRegistry


def test_rotation_boundaries_recovery_and_revoke(tmp_path):
    store = SessionStore(tmp_path / "client.sqlite", b"x" * 32, "server")
    pair = store.issue("alice", "CLI", "exchange-one", now=1000)
    assert store.authenticate(pair["access_token"], now=1001)["subject"] == "alice"
    with pytest.raises(ClientAccessError):
        store.authenticate(pair["refresh_token"], now=1001)
    with pytest.raises(ClientAccessError):
        store.authenticate(pair["access_token"], now=1000 + 30 * 86400)
    refreshed = store.rotate(pair["refresh_token"], "refresh-one", now=1000 + 31 * 86400)
    assert refreshed["generation"] == 2
    assert store.rotate(pair["refresh_token"], "refresh-one", now=1001 + 31 * 86400) == refreshed
    with pytest.raises(ClientAccessError):
        store.rotate(pair["refresh_token"], "other-key", now=1001 + 31 * 86400)
    store.revoke(refreshed["refresh_token"], now=1002 + 31 * 86400)
    with pytest.raises(ClientAccessError):
        store.authenticate(refreshed["access_token"], now=1003 + 31 * 86400)


def test_absolute_expiry_and_restart(tmp_path):
    path = tmp_path / "client.sqlite"
    store = SessionStore(path, b"x" * 32, "server")
    pair = store.issue("alice", "CLI", "exchange-one", now=0)
    for day in [80, 160, 240, 320, 364]:
        pair = store.rotate(pair["refresh_token"], f"refresh-{day}", now=day * 86400)
    assert pair["access_expires_at"] == pair["absolute_expires_at"]
    assert pair["refresh_expires_at"] == pair["absolute_expires_at"]
    store = SessionStore(path, b"x" * 32, "server")
    assert store.authenticate(pair["access_token"], now=364 * 86400)["subject"] == "alice"
    with pytest.raises(ClientAccessError):
        store.rotate(pair["refresh_token"], "expired", now=365 * 86400)
    assert not store.revoke_session("bob", pair["session_id"], now=364 * 86400)
    assert len(store.list_sessions("bob")) == 0
    assert b"sagc_" not in path.read_bytes()


def test_pairing_pkce_and_identity_binding():
    registry = PairingRegistry("https://nas/app/sag/settings", max_pending=1)
    verifier = "a" * 43
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    item = registry.create("CLI", challenge, now=0)
    with pytest.raises(ClientAccessError, match="authorization_pending"):
        registry.poll(item["device_code"], now=1)
    with pytest.raises(ClientAccessError, match="slow_down"):
        registry.poll(item["device_code"], now=2)
    registry.approve(item["user_code"], "alice", True, now=3)
    with pytest.raises(ClientAccessError):
        registry.approve(item["user_code"], "bob", True, now=4)
    ticket = registry.poll(item["device_code"], now=8)["exchange_ticket"]
    with pytest.raises(ClientAccessError):
        registry.exchange(ticket, "b" * 43, now=9)
    assert registry.exchange(ticket, verifier, now=9)["subject"] == "alice"
    with pytest.raises(ClientAccessError):
        registry.exchange(ticket, verifier, now=69)


def test_operation_recovery_is_subject_scoped(tmp_path):
    store = SessionStore(tmp_path / "client.sqlite", b"x" * 32, "server")
    op = store.begin_operation("alice", "write-key-1", "POST", "/sources", b'{"name":"one"}', now=1)
    assert op["status"] == "pending"
    assert store.operation("bob", "write-key-1") is None
    with pytest.raises(ClientAccessError, match="idempotency_conflict"):
        store.begin_operation("alice", "write-key-1", "POST", "/sources", b"other", now=2)
    store.complete_operation("alice", "write-key-1", 201, {"id": "source-1"})
    saved = store.begin_operation("alice", "write-key-1", "POST", "/sources", b'{"name":"one"}', now=3)
    assert saved["status"] == "completed"
    assert saved["response"] == {"id": "source-1"}


@pytest.mark.asyncio
async def test_final_business_boundary_and_multipart_recovery(tmp_path):
    import httpx
    from fastapi import FastAPI, UploadFile

    from sag_api.client_access.middleware import ClientBusinessMiddleware

    store = SessionStore(tmp_path / "client.sqlite", b"x" * 32, "server")
    pair = store.issue("alice", "CLI", "exchange-one")
    calls = []
    app = FastAPI()

    @app.post("/api/v1/sources/a/documents")
    async def upload(file: UploadFile):
        calls.append(await file.read())
        return {"id": "doc1", "source_id": "a", "filename": file.filename}

    async def authority(request):
        return store, store.authenticate(request.headers["authorization"].removeprefix("Bearer "))

    app.add_middleware(ClientBusinessMiddleware, authority=authority)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        headers = {"Authorization": "Bearer " + pair["access_token"], "Idempotency-Key": "upload-one"}
        first = await client.post(
            "/client/v1/api/sources/a/documents", files={"file": ("a.txt", b"hello", "text/plain")}, headers=headers
        )
        second = await client.post(
            "/client/v1/api/sources/a/documents", files={"file": ("a.txt", b"hello", "text/plain")}, headers=headers
        )
        assert first.status_code == second.status_code == 200
        assert first.json() == second.json()
        assert first.json()["document_id"] == "doc1"
        assert len(calls) == 1
        assert (await client.post("/client/v1/api/system/config", headers=headers)).status_code == 404
        store.revoke(pair["access_token"])
        assert (await client.post("/client/v1/api/sources/a/documents", headers=headers)).status_code == 401


def test_old_rotation_recovery_cannot_return_superseded_credentials(tmp_path):
    store = SessionStore(tmp_path / "client.sqlite", b"x" * 32, "server")
    first = store.issue("alice", "CLI", "exchange-one", now=0)
    second = store.rotate(first["refresh_token"], "refresh-one", now=1)
    third = store.rotate(second["refresh_token"], "refresh-two", now=2)
    with pytest.raises(ClientAccessError, match="recovery_superseded"):
        store.rotate(first["refresh_token"], "refresh-one", now=3)
    assert store.authenticate(third["access_token"], now=3)["generation"] == 3


@pytest.mark.asyncio
async def test_delete_204_has_no_body_on_real_tcp(tmp_path):
    import asyncio
    import socket

    import httpx
    import uvicorn
    from fastapi import FastAPI, Response

    from sag_api.client_access.middleware import ClientBusinessMiddleware

    store = SessionStore(tmp_path / "client.sqlite", b"x" * 32, "server")
    pair = store.issue("alice", "CLI", "exchange-one")
    app = FastAPI()

    @app.delete("/api/v1/sources/a")
    async def delete():
        return Response(status_code=204)

    async def authority(request):
        return store, store.authenticate(request.headers["authorization"].removeprefix("Bearer "))

    app.add_middleware(ClientBusinessMiddleware, authority=authority)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen()
    server = uvicorn.Server(uvicorn.Config(app, log_level="critical", lifespan="off"))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        for _ in range(100):
            if server.started:
                break
            await asyncio.sleep(0.01)
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{sock.getsockname()[1]}") as client:
            headers = {"Authorization": "Bearer " + pair["access_token"], "Idempotency-Key": "delete-one"}
            for _ in range(2):
                response = await client.delete("/client/v1/api/sources/a", headers=headers)
                assert response.status_code == 204
                assert response.content == b""
                assert "content-type" not in response.headers
                assert response.headers["X-Operation-Id"] == store.operation_id("alice", "delete-one")
    finally:
        server.should_exit = True
        await task


def test_pairing_recovery_window_outlives_pending_expiry():
    registry = PairingRegistry(None)
    verifier = "a" * 43
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    item = registry.create("CLI", challenge, now=0)
    registry.approve(item["user_code"], "alice", True, now=590)
    ticket = registry.poll(item["device_code"], now=595)["exchange_ticket"]
    registry.exchange(ticket, verifier, now=596)
    raw = registry.entries[next(iter(registry.entries))]
    raw["exchanged_at"] = 596
    assert registry.exchange(ticket, verifier, now=800)["subject"] == "alice"
    with pytest.raises(ClientAccessError):
        registry.poll(item["device_code"], now=800)
    with pytest.raises(ClientAccessError):
        registry.exchange(ticket, verifier, now=896)


def test_expired_refresh_only_recovers_an_existing_same_key_response(tmp_path):
    from sag_api.client_access.core import DAY

    store = SessionStore(tmp_path / "client.sqlite", b"x" * 32, "server")
    first = store.issue("alice", "CLI", "exchange-expiry", now=0)
    second = store.rotate(first["refresh_token"], "rotation-expiry", now=90 * DAY - 1)
    assert store.recover_rotation(first["refresh_token"], "rotation-expiry", now=90 * DAY + 1) == second
    assert store.recover_rotation(first["refresh_token"], "another-key", now=90 * DAY + 1) is None
    with pytest.raises(ClientAccessError):
        store.rotate(first["refresh_token"], "another-key", now=90 * DAY + 1)
    assert store.recover_rotation(first["refresh_token"], "rotation-expiry", now=90 * DAY + 300) is None


def test_signature_only_expired_routing_never_authenticates_or_skips_purpose(tmp_path):
    from sag_api.client_access.core import DAY

    store = SessionStore(tmp_path / "client.sqlite", b"x" * 32, "server")
    pair = store.issue("alice", "CLI", "exchange-routing", now=0)
    assert (
        store.codec.decode(pair["refresh_token"], purpose="refresh", now=91 * DAY, allow_expired=True)["subject"]
        == "alice"
    )
    with pytest.raises(ClientAccessError):
        store.codec.decode(pair["refresh_token"], purpose="access", now=91 * DAY, allow_expired=True)
    with pytest.raises(ClientAccessError):
        store.authenticate(pair["refresh_token"], purpose="refresh", now=91 * DAY)
    with pytest.raises(ClientAccessError):
        store.codec.decode(pair["refresh_token"] + "tampered", purpose="refresh", now=91 * DAY, allow_expired=True)
