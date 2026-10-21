"""Standard deployment identity adapter for the deployment-neutral client-v1 authority."""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

from fastapi import APIRouter, Depends, Request, Response
from filelock import FileLock
from starlette.responses import JSONResponse

from sag_api.client_access.core import ClientAccessError, SessionStore, digest
from sag_api.client_access.middleware import ClientBusinessMiddleware
from sag_api.client_access.pairing import PairingRegistry
from sag_api.core.config import settings
from sag_api.core.db import SessionLocal
from sag_api.core.deps import get_current_user
from sag_api.db.models import User
from sag_api.services.auth_service import get_user

router = APIRouter()


class StandardAuthority:
    def __init__(self):
        directory = Path(settings.data_dir) / "client-access"
        directory.mkdir(parents=True, exist_ok=True)
        key_file = directory / "authority.key"
        with FileLock(str(directory / "authority.lock")):
            if key_file.exists():
                key = key_file.read_bytes()
            else:
                descriptor = os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(descriptor, "wb") as file:
                    key = os.urandom(32)
                    file.write(key)
                    file.flush()
                    os.fsync(file.fileno())
        if len(key) != 32:
            raise RuntimeError("Invalid client authority key; restore the persisted key from backup")
        self.server_id = "sag-" + hashlib.sha256(key).hexdigest()[:24]
        self.store = SessionStore(directory / "sessions.sqlite", key, self.server_id)
        web_base = settings.client_web_url
        self.verification_uri = f"{web_base}/settings?tab=integrations" if web_base else None
        self.pairings = PairingRegistry(self.verification_uri)

    async def authenticate(self, request: Request):
        authorization = request.headers.get("authorization", "")
        if not authorization.startswith("Bearer "):
            raise ClientAccessError("invalid_grant", 401)
        claims = self.store.authenticate(authorization[7:])
        if settings.auth_mode != "password" or claims.get("route", {}).get("auth_mode") != "password":
            raise ClientAccessError("identity_changed", 401)
        async with SessionLocal() as session:
            user = await get_user(session, claims["subject"])
            if user is None or not user.is_active or not user.email:
                raise ClientAccessError("identity_changed", 401)
        return self.store, claims

    async def exchange(self, ticket: str, verifier: str, key: str):
        if not isinstance(key, str) or not 8 <= len(key) <= 200:
            raise ClientAccessError("invalid_idempotency_key")
        entry = self.store.exchange_entry(ticket, verifier, key)
        recovering = entry is not None
        bound = digest(ticket + ":" + key)
        if not recovering:
            with self.pairings.lock:
                entry = self.pairings.exchange(ticket, verifier)
                raw = next(item for item in self.pairings.entries.values() if item.get("ticket") == ticket)
                if raw.get("exchange_key") not in (None, bound):
                    raise ClientAccessError("invalid_grant")
                raw["exchange_key"] = bound
                raw.setdefault("exchanged_at", time.time())
            self.store.exchange_entry(ticket, verifier, key, entry)
        if settings.auth_mode != "password":
            raise ClientAccessError("identity_changed", 401)
        async with SessionLocal() as session:
            user = await get_user(session, entry["subject"])
            if user is None or not user.is_active or not user.email:
                raise ClientAccessError("identity_changed", 401)
        return self.store.issue(
            entry["subject"], entry["client_name"], bound, claims=entry["claims"], approved_at=entry["approved_at"]
        )


def authority(request: Request) -> StandardAuthority:
    current = getattr(request.app.state, "client_authority", None)
    if current is None:
        current = StandardAuthority()
        request.app.state.client_authority = current
    return current


async def authenticate_business(request: Request):
    return await authority(request).authenticate(request)


async def _payload(request: Request) -> dict:
    content = bytearray()
    async for chunk in request.stream():
        content.extend(chunk)
        if len(content) > 16384:
            raise ClientAccessError("request_too_large", 413)
    try:
        body = json.loads(content)
        if not isinstance(body, dict):
            raise ValueError
        return body
    except (ValueError, UnicodeError):
        raise ClientAccessError("invalid_request") from None


async def _web_user(request: Request, user: User = Depends(get_current_user)) -> User:
    if settings.auth_mode != "password" or not user.email or getattr(request.state, "auth_kind", None) != "jwt":
        raise ClientAccessError("password_authentication_required", 403)
    return user


@router.get("/client/v1/info")
async def info(request: Request):
    current = authority(request)
    return {
        "protocol": "client-v1",
        "deployment": "standard",
        "server_id": current.server_id,
        "api_base_path": "/client/v1/api",
        "auth_methods": ["device_code"],
        "verification_uri": current.verification_uri,
    }


@router.post("/client/v1/device/code")
async def device_code(request: Request):
    body = await _payload(request)
    if body.get("code_challenge_method") != "S256":
        raise ClientAccessError("invalid_request")
    return authority(request).pairings.create(body.get("client_name"), body.get("code_challenge"))


@router.post("/client/v1/token")
async def token(request: Request):
    body = await _payload(request)
    grant = body.get("grant_type")
    current = authority(request)
    key = request.headers.get("idempotency-key", "")
    if grant == "urn:ietf:params:oauth:grant-type:device_code":
        if not isinstance(body.get("device_code"), str):
            raise ClientAccessError("invalid_request")
        return current.pairings.poll(body["device_code"])
    if grant == "urn:sag:params:oauth:grant-type:device-ticket":
        if not isinstance(body.get("exchange_ticket"), str) or not isinstance(body.get("code_verifier"), str):
            raise ClientAccessError("invalid_request")
        return await current.exchange(body["exchange_ticket"], body["code_verifier"], key)
    if grant == "refresh_token":
        refresh = body.get("refresh_token")
        if not isinstance(refresh, str):
            raise ClientAccessError("invalid_request")
        recovered = current.store.recover_rotation(refresh, key)
        claims = current.store.codec.decode(recovered["refresh_token"] if recovered else refresh, purpose="refresh")
        if settings.auth_mode != "password" or claims.get("route", {}).get("auth_mode") != "password":
            raise ClientAccessError("identity_changed", 401)
        async with SessionLocal() as session:
            user = await get_user(session, claims["subject"])
            if user is None or not user.is_active or not user.email:
                raise ClientAccessError("identity_changed", 401)
        return recovered if recovered is not None else current.store.rotate(refresh, key)
    raise ClientAccessError("unsupported_grant_type")


@router.get("/client/v1/session")
async def session_info(request: Request):
    store, claims = await authority(request).authenticate(request)
    record = next(item for item in store.list_sessions(claims["subject"]) if item["session_id"] == claims["session_id"])
    async with SessionLocal() as session:
        user = await get_user(session, claims["subject"])
        if user is None or not user.is_active:
            raise ClientAccessError("identity_changed", 401)
        return {**record, "user": {"id": user.id, "name": user.name}}


@router.post("/client/v1/session/revoke", status_code=204)
async def revoke(request: Request):
    bearer = request.headers.get("authorization", "")
    if not bearer.startswith("Bearer "):
        raise ClientAccessError("invalid_grant", 401)
    authority(request).store.revoke(bearer[7:])
    return Response(status_code=204)


@router.get("/client/v1/operations/{key}")
async def operation(key: str, request: Request):
    store, claims = await authority(request).authenticate(request)
    result = store.operation(claims["subject"], key)
    if result is None:
        raise ClientAccessError("operation_not_found", 404)
    return result


@router.get("/api/v1/client-access/info")
async def web_info(request: Request, _user: User = Depends(_web_user)):
    current = authority(request)
    return {
        "protocol": "client-v1",
        "deployment": "standard",
        "server_id": current.server_id,
        "api_url": settings.client_public_url,
        "verification_uri": current.verification_uri,
        "scope": "knowledge:read knowledge:write",
        "access_days": 30,
        "refresh_days": 90,
        "absolute_days": 365,
    }


@router.get("/api/v1/client-access/device/{user_code}")
async def inspect_device(user_code: str, request: Request, _user: User = Depends(_web_user)):
    registry = authority(request).pairings
    with registry.lock:
        registry._prune(time.time())
        entry = next(
            (item for item in registry.entries.values() if item["user_code"] == user_code.upper().replace("-", "")),
            None,
        )
        if entry is None or entry["state"] != "pending":
            raise ClientAccessError("expired_token", 404)
        return {
            "client_name": entry["client_name"],
            "user_code": entry["user_code"],
            "scope": "knowledge:read knowledge:write",
            "expires_in": max(0, int(entry["expires"] - time.time())),
        }


@router.post("/api/v1/client-access/inspect")
async def inspect_device_post(request: Request, user: User = Depends(_web_user)):
    body = await _payload(request)
    if not isinstance(body.get("user_code"), str):
        raise ClientAccessError("invalid_request")
    return {**(await inspect_device(body["user_code"], request, user)), "user_name": user.name}


@router.post("/api/v1/client-access/approve")
async def approve(request: Request, user: User = Depends(_web_user)):
    body = await _payload(request)
    if not isinstance(body.get("user_code"), str) or not isinstance(body.get("approve"), bool):
        raise ClientAccessError("invalid_request")
    authority(request).pairings.approve(body["user_code"], user.id, body["approve"], claims={"auth_mode": "password"})
    return {"approved": body["approve"]}


@router.get("/api/v1/client-access/sessions")
async def sessions(request: Request, user: User = Depends(_web_user)):
    return authority(request).store.list_sessions(user.id)


@router.delete("/api/v1/client-access/sessions/{session_id}", status_code=204)
async def delete_session(session_id: str, request: Request, user: User = Depends(_web_user)):
    if not authority(request).store.revoke_session(user.id, session_id):
        raise ClientAccessError("session_not_found", 404)
    return Response(status_code=204)


def install(app):
    app.include_router(router)
    app.add_middleware(ClientBusinessMiddleware, authority=authenticate_business)

    @app.exception_handler(ClientAccessError)
    async def error_handler(_request, error):
        return JSONResponse({"error": error.code}, status_code=error.status)
