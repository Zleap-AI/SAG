"""Bounded deployment-neutral device pairing registry; no identity before approval."""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import time
from threading import RLock

from .core import ClientAccessError, digest


class PairingRegistry:
    def __init__(self, verification_uri: str | None, max_pending: int = 256):
        self.verification_uri, self.max_pending = verification_uri, max_pending
        self.entries: dict[str, dict] = {}
        self.lock = RLock()

    def _prune(self, now):
        self.entries = {
            k: v
            for k, v in self.entries.items()
            if max(v["expires"], v.get("ticket_exp", 0), v.get("exchanged_at", -300) + 300) > now
        }

    def create(self, client_name: str, challenge: str, *, now=None) -> dict:
        now = time.time() if now is None else now
        if not isinstance(client_name, str) or not 1 <= len(client_name.strip()) <= 120:
            raise ClientAccessError("invalid_request")
        if not isinstance(challenge, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", challenge):
            raise ClientAccessError("invalid_request")
        with self.lock:
            self._prune(now)
            if len(self.entries) >= self.max_pending:
                raise ClientAccessError("temporarily_unavailable", 429)
            code, user_code = secrets.token_urlsafe(32), secrets.token_hex(4).upper()
            self.entries[digest(code)] = {
                "client_name": client_name,
                "challenge": challenge,
                "user_code": user_code,
                "expires": now + 600,
                "last_poll": None,
                "state": "pending",
            }
            return {
                "device_code": code,
                "user_code": user_code,
                "verification_uri": self.verification_uri,
                "expires_in": 600,
                "interval": 5,
            }

    def approve(self, user_code: str, subject: str, approve: bool, *, claims=None, now=None):
        now = time.time() if now is None else now
        with self.lock:
            self._prune(now)
            entry = next(
                (v for v in self.entries.values() if v["user_code"] == user_code.upper().replace("-", "")), None
            )
            if entry is None or entry["state"] != "pending":
                raise ClientAccessError("invalid_grant")
            entry.update(
                subject=subject, claims=claims or {}, approved_at=now, state="approved" if approve else "denied"
            )

    def poll(self, device_code: str, *, now=None) -> dict:
        now = time.time() if now is None else now
        with self.lock:
            self._prune(now)
            entry = self.entries.get(digest(device_code))
            if entry is None or entry["expires"] <= now:
                raise ClientAccessError("expired_token")
            if entry["last_poll"] is not None and now - entry["last_poll"] < 5:
                raise ClientAccessError("slow_down")
            entry["last_poll"] = now
            if entry["state"] == "denied":
                raise ClientAccessError("access_denied")
            if entry["state"] == "pending":
                raise ClientAccessError("authorization_pending")
            if "ticket" not in entry:
                entry.update(ticket=secrets.token_urlsafe(32), ticket_exp=now + 60)
            if entry["ticket_exp"] <= now:
                raise ClientAccessError("expired_token")
            return {"exchange_ticket": entry["ticket"], "expires_in": int(entry["ticket_exp"] - now)}

    def exchange(self, ticket: str, verifier: str, *, now=None) -> dict:
        now = time.time() if now is None else now
        with self.lock:
            self._prune(now)
            entry = next((v for v in self.entries.values() if v.get("ticket") == ticket), None)
            if (
                entry is None
                or max(entry["ticket_exp"], entry.get("exchanged_at", -300) + 300) <= now
                or not re.fullmatch(r"[A-Za-z0-9._~-]{43,128}", verifier)
            ):
                raise ClientAccessError("invalid_grant")
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
            if not hmac.compare_digest(challenge, entry["challenge"]):
                raise ClientAccessError("invalid_grant")
            return {
                "subject": entry["subject"],
                "client_name": entry["client_name"],
                "claims": entry["claims"],
                "approved_at": entry["approved_at"],
            }
