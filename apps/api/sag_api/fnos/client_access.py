"""fnOS adapter: host approval, UID routing, persistent policy and tenant stores.

No host account-instance lifecycle query is configured by default.
The default authority is the existing SAG account instance, checked on every call.
OS account lifecycle synchronization remains an optional host integration.
"""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
import time
from pathlib import Path
from urllib.parse import urlsplit

from sag_api.client_access.core import ClientAccessError, SessionStore, TokenCodec, digest
from sag_api.client_access.pairing import PairingRegistry
from sag_api.fnos.identity import GatewayIdentity
from sag_api.fnos.workspace import WorkspacePaths


class FnOSClientAccess:
    def __init__(self, data_root: Path, key: bytes, *, account_verifier=None):
        self.root, self.key = Path(data_root), key
        self.root.mkdir(parents=True, exist_ok=True)
        self.policy_file = self.root / "client-access-policy.json"
        if not self.policy_file.exists():
            self._write_policy(
                {"server_id": secrets.token_hex(16), "enabled": False, "api_url": None, "verification_uri": None}
            )
        self.host_account_verifier = account_verifier
        self.account_verifier = self._local_account
        self.pairings = PairingRegistry(self.policy()["verification_uri"])

    def policy(self):
        return json.loads(self.policy_file.read_text())

    def _write_policy(self, value):
        temporary = self.policy_file.with_suffix("." + secrets.token_hex(6) + ".tmp")
        fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.policy_file)

    def update_policy(self, identity: GatewayIdentity, values: dict):
        if not identity.is_admin:
            raise ClientAccessError("administrator_required", 403)
        existing = self.policy()
        for name in ("api_url", "verification_uri"):
            value = values.get(name)
            if value:
                parsed = urlsplit(value)
                if (
                    parsed.scheme not in {"http", "https"}
                    or not parsed.hostname
                    or parsed.username
                    or parsed.password
                    or parsed.query
                    or parsed.fragment
                ):
                    raise ClientAccessError("invalid_request")
            existing[name] = value or None
        existing["enabled"] = values.get("enabled") is True
        if existing["enabled"] and self.account_verifier is None:
            raise ClientAccessError("host_identity_unavailable", 503)
        self._write_policy(existing)
        self.pairings.verification_uri = existing["verification_uri"]
        return self.metadata(identity)

    def metadata(self, identity):
        return {
            **self.policy(),
            "protocol": "client-v1",
            "deployment": "fnos",
            "can_manage_policy": identity.is_admin,
            "host_identity_available": True,
            "host_lifecycle_synchronized": self.host_account_verifier is not None,
            "access_days": 30,
            "refresh_days": 90,
            "absolute_days": 365,
        }

    def codec(self):
        return TokenCodec(self.key, self.policy()["server_id"])

    def store(self, identity):
        paths = WorkspacePaths.for_identity(self.root, self.root / ".client-temp", identity)
        paths.prepare()
        return SessionStore(paths.database_file.parent / "client-access.sqlite", self.key, self.policy()["server_id"])

    async def _local_account(self, identity):
        paths = WorkspacePaths.for_identity(self.root, self.root / ".client-temp", identity)
        try:
            with sqlite3.connect(f"file:{paths.database_file}?mode=ro", uri=True) as db:
                row = db.execute(
                    "SELECT created_at,auth_version,password_hash,is_active,name FROM users WHERE id=?",
                    (f"fnos_{identity.uid}",),
                ).fetchone()
        except sqlite3.Error:
            return None
        if row is None or not row[3] or row[4] != (identity.username or f"fnos_{identity.uid}"):
            return None
        return digest(json.dumps(list(row[:3])))

    async def account(self, identity):
        if self.account_verifier is None:
            raise ClientAccessError("host_identity_unavailable", 503)
        try:
            instance = await self.account_verifier(identity)
        except Exception:
            raise ClientAccessError("host_identity_unavailable", 503) from None
        if not isinstance(instance, str) or not instance:
            raise ClientAccessError("host_identity_unavailable", 503)
        return instance

    async def external_account(self, identity):
        if self.host_account_verifier is None:
            raise ClientAccessError("host_identity_unavailable", 503)
        try:
            instance = await self.host_account_verifier(identity)
        except Exception:
            raise ClientAccessError("host_identity_unavailable", 503) from None
        if not isinstance(instance, str) or not instance:
            raise ClientAccessError("host_identity_unavailable", 503)
        return instance

    async def validate_account(self, identity, route):
        if await self.account(identity) != route.get("account_instance"):
            raise ClientAccessError("host_identity_changed", 401)
        external = route.get("external_account_instance")
        if external is not None or self.host_account_verifier is not None:
            if await self.external_account(identity) != external:
                raise ClientAccessError("host_identity_changed", 401)

    async def approve(self, identity, user_code, approve):
        instance = await self.account(identity) if approve else ""
        claims = {"uid": identity.uid, "username": identity.username, "account_instance": instance}
        if approve and self.host_account_verifier is not None:
            claims["external_account_instance"] = await self.external_account(identity)
        self.pairings.approve(
            user_code,
            f"fnos_{identity.uid}",
            approve,
            claims=claims,
        )

    async def exchange(self, ticket, verifier, idempotency_key):
        recovery = SessionStore(self.root / "client-access-gateway.sqlite", self.key, self.policy()["server_id"])
        entry = recovery.exchange_entry(ticket, verifier, idempotency_key)
        recovering = entry is not None
        if entry is None:
            entry = self.pairings.exchange(ticket, verifier)
        route = entry["claims"]
        identity = GatewayIdentity(route["uid"], route["username"], False)
        await self.validate_account(identity, route)
        # Ticket is single-use across distinct request keys; retry keeps the same key.
        bound = digest(ticket + ":" + idempotency_key)
        if not recovering:
            with self.pairings.lock:
                raw = next(v for v in self.pairings.entries.values() if v.get("ticket") == ticket)
                if raw.get("exchange_key") not in (None, bound):
                    raise ClientAccessError("invalid_grant")
                raw["exchange_key"] = bound
                raw.setdefault("exchanged_at", time.time())
            recovery.exchange_entry(ticket, verifier, idempotency_key, entry)
        return self.store(identity).issue(
            entry["subject"], entry["client_name"], bound, claims=route, approved_at=entry["approved_at"]
        )

    async def route(self, token, purpose):
        claims = self.codec().decode(token, purpose=purpose)
        route = claims["route"]
        try:
            identity = GatewayIdentity(int(route["uid"]), route["username"], False)
            if identity.uid <= 0 or claims["subject"] != f"fnos_{identity.uid}":
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise ClientAccessError("invalid_grant", 401) from None
        await self.validate_account(identity, route)
        # Worker repeats final authorization against the same tenant authority.
        return identity
