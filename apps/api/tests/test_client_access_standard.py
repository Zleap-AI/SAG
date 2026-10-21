"""Standard deployment pairing exercises real HTTP handlers, authority and durable store."""

import base64
import hashlib
import secrets
from uuid import uuid4

import httpx
import pytest

from sag_api.core.config import settings
from sag_api.core.db import init_db
from sag_api.main import create_app


@pytest.fixture
async def standard_client(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "auth_mode", "password")
    monkeypatch.setattr(settings, "allow_registration", True)
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    await init_db()
    from sqlalchemy import delete, select

    from sag_api.core.db import SessionLocal
    from sag_api.db.models import User

    async with SessionLocal() as database:
        before = list((await database.scalars(select(User.id))).all())
    app = create_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        try:
            yield client, app
        finally:
            async with SessionLocal() as database:
                await database.execute(delete(User).where(User.id.not_in(before)))
                await database.commit()


async def web_user(client):
    response = await client.post(
        "/api/v1/auth/register", json={"email": f"{uuid4().hex}@example.test", "password": "password123"}
    )
    assert response.status_code == 201, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def pair(client, web, app=None):
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    created = await client.post(
        "/client/v1/device/code",
        json={"client_name": "Automation laptop", "code_challenge": challenge, "code_challenge_method": "S256"},
    )
    assert created.status_code == 200, created.text
    code = created.json()
    approved = await client.post(
        "/api/v1/client-access/approve", headers=web, json={"user_code": code["user_code"], "approve": True}
    )
    assert approved.status_code == 200, approved.text
    polled = await client.post(
        "/client/v1/token",
        json={"grant_type": "urn:ietf:params:oauth:grant-type:device_code", "device_code": code["device_code"]},
    )
    ticket = polled.json()["exchange_ticket"]
    request = {
        "grant_type": "urn:sag:params:oauth:grant-type:device-ticket",
        "exchange_ticket": ticket,
        "code_verifier": verifier,
    }
    headers = {"Idempotency-Key": uuid4().hex}
    result = await client.post("/client/v1/token", headers=headers, json=request)
    assert result.status_code == 200, result.text
    if app is not None:
        app.state.client_authority = None
    assert (await client.post("/client/v1/token", headers=headers, json=request)).json() == result.json()
    conflict = await client.post("/client/v1/token", headers={"Idempotency-Key": uuid4().hex}, json=request)
    assert conflict.status_code in {400, 409}
    return result.json()


async def test_standard_session_routes_revoke_and_management_boundaries(standard_client):
    client, _app = standard_client
    info = await client.get("/client/v1/info")
    assert info.status_code == 200, info.text
    assert info.json()["protocol"] == "client-v1"
    assert info.json()["deployment"] == "standard"
    assert info.json()["verification_uri"] is None
    web = await web_user(client)
    other = await web_user(client)
    record = await pair(client, web)
    access = {"Authorization": f"Bearer {record['access_token']}"}
    refresh = {"Authorization": f"Bearer {record['refresh_token']}"}
    status = await client.get("/client/v1/session", headers=access)
    assert status.status_code == 200
    current_user = (await client.get("/api/v1/auth/me", headers=web)).json()
    assert status.json()["user"] == {"id": current_user["id"], "name": current_user["name"]}
    assert (await client.get("/client/v1/session", headers=refresh)).status_code == 401
    assert (await client.get("/client/v1/api/sources", headers=access)).status_code == 200
    assert (await client.get("/client/v1/api/sources", headers=refresh)).status_code == 401
    assert (await client.get("/client/v1/api/system/model-config", headers=access)).status_code == 404
    assert (await client.get("/api/v1/client-access/sessions", headers=access)).status_code == 401
    sessions = (await client.get("/api/v1/client-access/sessions", headers=web)).json()
    assert [item["session_id"] for item in sessions] == [record["session_id"]]
    assert (await client.get("/api/v1/client-access/sessions", headers=other)).json() == []
    assert (
        await client.delete(f"/api/v1/client-access/sessions/{record['session_id']}", headers=other)
    ).status_code == 404
    assert (await client.post("/client/v1/session/revoke", headers=refresh)).status_code == 204
    assert (await client.get("/client/v1/api/sources", headers=access)).status_code == 401


async def test_standard_rotation_is_recoverable_and_never_grants_management(standard_client):
    client, _app = standard_client
    record = await pair(client, await web_user(client))
    request = {"grant_type": "refresh_token", "refresh_token": record["refresh_token"]}
    headers = {"Idempotency-Key": uuid4().hex}
    rotated = await client.post("/client/v1/token", json=request, headers=headers)
    assert rotated.status_code == 200, rotated.text
    assert rotated.json()["generation"] == 2
    assert (await client.post("/client/v1/token", json=request, headers=headers)).json() == rotated.json()
    assert (
        await client.get("/client/v1/session", headers={"Authorization": f"Bearer {record['access_token']}"})
    ).status_code == 401
    assert (
        await client.post("/client/v1/token", json=request, headers={"Idempotency-Key": uuid4().hex})
    ).status_code == 401


async def test_names_only_web_identity_cannot_approve_remote_devices(standard_client, monkeypatch):
    client, _app = standard_client
    monkeypatch.setattr(settings, "auth_mode", "local")
    logged_in = await client.post("/api/v1/auth/login", json={"name": f"person-{uuid4().hex}"})
    assert logged_in.status_code == 200
    result = await client.post(
        "/api/v1/client-access/approve",
        headers={"Authorization": f"Bearer {logged_in.json()['access_token']}"},
        json={"user_code": "12345678", "approve": True},
    )
    assert result.status_code == 403


async def test_auth_payloads_are_bounded_and_unsupported_grants_are_explicit(standard_client):
    client, _app = standard_client
    huge = await client.post("/client/v1/token", content=b"x" * 17000, headers={"Content-Type": "application/json"})
    assert huge.status_code == 413
    result = await client.post("/client/v1/token", json={"grant_type": "password"})
    assert result.status_code == 400
    assert result.json()["error"] == "unsupported_grant_type"


async def test_real_source_writes_have_recoverable_subject_scoped_operations(standard_client):
    client, app = standard_client
    web = await web_user(client)
    peer = await web_user(client)
    record = await pair(client, web)
    other = await pair(client, peer)
    headers = {"Authorization": f"Bearer {record['access_token']}", "Idempotency-Key": uuid4().hex}
    async with app.router.lifespan_context(app):
        first = await client.post("/client/v1/api/sources", json={"name": "Recoverable source"}, headers=headers)
        assert first.status_code == 201, first.text
        repeated = await client.post("/client/v1/api/sources", json={"name": "Recoverable source"}, headers=headers)
        assert repeated.json() == first.json()
        source_id = first.json()["id"]
        assert first.json()["operation_id"]
        operation = await client.get(f"/client/v1/operations/{headers['Idempotency-Key']}", headers=headers)
        assert operation.json()["status"] == "completed"
        assert operation.json()["response"]["id"] == source_id
        assert (
            await client.get(
                f"/client/v1/operations/{headers['Idempotency-Key']}",
                headers={"Authorization": f"Bearer {other['access_token']}"},
            )
        ).status_code == 404
        conflict = await client.post("/client/v1/api/sources", json={"name": "Changed payload"}, headers=headers)
        assert conflict.status_code == 409
        read = await client.get(f"/client/v1/api/sources/{source_id}", headers=headers)
        assert read.status_code == 200
        patched = await client.patch(
            f"/client/v1/api/sources/{source_id}",
            json={"name": "Renamed source"},
            headers={**headers, "Idempotency-Key": uuid4().hex},
        )
        assert patched.status_code == 200, patched.text
        upload_headers = {**headers, "Idempotency-Key": uuid4().hex}
        upload = await client.post(
            f"/client/v1/api/sources/{source_id}/documents",
            files={"file": ("example.md", b"# offline test\nKnowledge content.", "text/markdown")},
            headers=upload_headers,
        )
        assert upload.status_code == 201, upload.text
        assert upload.json()["job_id"]
        repeated_upload = await client.post(
            f"/client/v1/api/sources/{source_id}/documents",
            files={"file": ("example.md", b"# offline test\nKnowledge content.", "text/markdown")},
            headers=upload_headers,
        )
        assert repeated_upload.json() == upload.json()
        upload_job = await client.get(f"/client/v1/api/jobs/{upload.json()['job_id']}", headers=headers)
        assert upload_job.json()["document_id"] == upload.json()["id"]
        ingest = await client.post(
            f"/client/v1/api/sources/{source_id}/documents/ingest",
            json={"title": "Inline text", "text": "Offline ingestion is queued."},
            headers={**headers, "Idempotency-Key": uuid4().hex},
        )
        assert ingest.status_code == 201, ingest.text
        assert ingest.json()["job_id"]
        deleted = await client.delete(
            f"/client/v1/api/sources/{source_id}", headers={**headers, "Idempotency-Key": uuid4().hex}
        )
        assert deleted.status_code == 202, deleted.text
        job = await client.get(f"/client/v1/api/jobs/{deleted.json()['id']}", headers=headers)
        assert job.status_code == 200
        import asyncio

        for _ in range(100):
            job = await client.get(f"/client/v1/api/jobs/{deleted.json()['id']}", headers=headers)
            if job.json()["status"] in {"succeeded", "failed"}:
                break
            await asyncio.sleep(0.05)
        assert job.json()["status"] == "succeeded", job.text


async def test_disabled_user_loses_business_and_refresh_access(standard_client):
    from sag_api.core.db import SessionLocal
    from sag_api.db.models import User

    client, _app = standard_client
    record = await pair(client, await web_user(client))
    claims = _app.state.client_authority.store.codec.decode(record["access_token"], purpose="access")
    async with SessionLocal() as session:
        user = await session.get(User, claims["subject"])
        user.is_active = False
        await session.commit()
    headers = {"Authorization": f"Bearer {record['access_token']}"}
    assert (await client.get("/client/v1/api/sources", headers=headers)).status_code == 401
    refreshed = await client.post(
        "/client/v1/token",
        json={"grant_type": "refresh_token", "refresh_token": record["refresh_token"]},
        headers={"Idempotency-Key": uuid4().hex},
    )
    assert refreshed.status_code == 401


async def test_completed_ticket_exchange_recovers_after_authority_restart(standard_client):
    client, app = standard_client
    record = await pair(client, await web_user(client), app=app)
    sessions = app.state.client_authority.store.list_sessions(
        app.state.client_authority.store.codec.decode(record["access_token"], purpose="access")["subject"]
    )
    assert len(sessions) == 1


async def test_refresh_response_loss_recovers_after_old_refresh_expiry_within_window(standard_client, monkeypatch):
    import time

    from sag_api.client_access import core

    client, app = standard_client
    web = await web_user(client)
    user = (await client.get("/api/v1/auth/me", headers=web)).json()
    await client.get("/client/v1/info")
    current = app.state.client_authority
    clock = [time.time()]
    monkeypatch.setattr(core, "_now", lambda now: clock[0] if now is None else now)
    old = current.store.issue(
        user["id"],
        "Near-expiry device",
        uuid4().hex,
        now=clock[0] - 90 * core.DAY + 1,
        claims={"auth_mode": "password"},
    )
    request = {"grant_type": "refresh_token", "refresh_token": old["refresh_token"]}
    key = {"Idempotency-Key": uuid4().hex}
    issued = await client.post("/client/v1/token", json=request, headers=key)
    assert issued.status_code == 200, issued.text
    clock[0] += 2
    recovered = await client.post("/client/v1/token", json=request, headers=key)
    assert recovered.status_code == 200, recovered.text
    assert recovered.json() == issued.json()
    from sag_api.core.db import SessionLocal
    from sag_api.db.models import User

    async with SessionLocal() as database:
        actor = await database.get(User, user["id"])
        actor.is_active = False
        await database.commit()
    assert (await client.post("/client/v1/token", json=request, headers=key)).status_code == 401
    async with SessionLocal() as database:
        actor = await database.get(User, user["id"])
        actor.is_active = True
        await database.commit()
    rejected = await client.post("/client/v1/token", json=request, headers={"Idempotency-Key": uuid4().hex})
    assert rejected.status_code == 401
    clock[0] += 300
    expired_recovery = await client.post("/client/v1/token", json=request, headers=key)
    assert expired_recovery.status_code == 401
