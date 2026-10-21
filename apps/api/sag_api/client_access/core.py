"""Portable client session authority. No Web or deployment-specific identity imports.

SQLite IMMEDIATE transactions serialize rotation across processes. Ordinary rows
contain token digests only; bounded recovery responses use authenticated encryption.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import sqlite3
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from cryptography.fernet import Fernet

DAY = 86400
SCOPE = "knowledge:read knowledge:write"


class ClientAccessError(ValueError):
    def __init__(self, code: str, status: int = 400):
        super().__init__(code)
        self.code, self.status = code, status


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def _now(now: float | None) -> float:
    return time.time() if now is None else now


def _date(value: float) -> str:
    return datetime.fromtimestamp(value, UTC).isoformat().replace("+00:00", "Z")


class TokenCodec:
    def __init__(self, key: bytes, audience: str):
        if len(key) < 32 or not audience:
            raise ValueError("key and audience required")
        self.key, self.audience = key, audience

    def encode(self, claims: dict) -> str:
        payload = _b64(
            json.dumps(
                {**claims, "aud": self.audience, "nonce": secrets.token_urlsafe(32)}, separators=(",", ":")
            ).encode()
        )
        signature = _b64(hmac.digest(self.key, payload.encode(), "sha256"))
        return f"sagc_{payload}.{signature}"

    def decode(self, token: str, *, purpose: str, now: float | None = None, allow_expired: bool = False) -> dict:
        """Verify routing claims; allow_expired never bypasses final session authentication."""
        try:
            if len(token) > 8192 or not token.startswith("sagc_"):
                raise ValueError
            payload, signature = token[5:].split(".")
            if not hmac.compare_digest(signature, _b64(hmac.digest(self.key, payload.encode(), "sha256"))):
                raise ValueError
            claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
            if (
                claims["aud"] != self.audience
                or claims["purpose"] != purpose
                or (not allow_expired and claims["exp"] <= _now(now))
            ):
                raise ValueError
            if not isinstance(claims["subject"], str) or not isinstance(claims["session_id"], str):
                raise ValueError
            return claims
        except (ValueError, TypeError, KeyError, UnicodeError):
            raise ClientAccessError("invalid_grant", 401) from None


class SessionStore:
    def __init__(self, path: Path, key: bytes, audience: str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.codec = TokenCodec(key, audience)
        self.cipher = Fernet(base64.urlsafe_b64encode(hmac.digest(key, b"client-recovery-v1", "sha256")))
        with self._db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS client_sessions(
                  id TEXT PRIMARY KEY, subject TEXT NOT NULL, client_name TEXT NOT NULL,
                  claims TEXT NOT NULL, approved REAL NOT NULL, absolute REAL NOT NULL,
                  access_exp REAL NOT NULL, refresh_exp REAL NOT NULL, generation INTEGER NOT NULL,
                  access_digest TEXT NOT NULL, refresh_digest TEXT NOT NULL, revoked REAL, last_used REAL);
                CREATE TABLE IF NOT EXISTS client_issuance(key TEXT PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS client_tickets(
                  key TEXT PRIMARY KEY, binding TEXT NOT NULL, entry BLOB NOT NULL, expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS client_recovery(
                  key TEXT PRIMARY KEY, binding TEXT NOT NULL, response BLOB NOT NULL, expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS client_operations(
                  key TEXT PRIMARY KEY, binding TEXT NOT NULL, status INTEGER, response BLOB, created REAL NOT NULL);
            """)
        self.path.chmod(0o600)

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def _recover(self, db, key: str, binding: str, now: float):
        if not isinstance(key, str) or not 8 <= len(key) <= 200:
            raise ClientAccessError("invalid_idempotency_key")
        db.execute("DELETE FROM client_recovery WHERE expires <= ?", (now,))
        row = db.execute("SELECT * FROM client_recovery WHERE key=?", (digest(key),)).fetchone()
        if row:
            if not hmac.compare_digest(row["binding"], binding):
                raise ClientAccessError("idempotency_conflict", 409)
            response = json.loads(self.cipher.decrypt(row["response"]))
            current = db.execute(
                "SELECT revoked,generation FROM client_sessions WHERE id=?", (response["session_id"],)
            ).fetchone()
            if current is None or current["revoked"] is not None:
                raise ClientAccessError("invalid_grant", 401)
            if current["generation"] != response["generation"]:
                raise ClientAccessError("recovery_superseded", 409)
            return response
        return None

    def _save_recovery(self, db, key: str, binding: str, response: dict, now: float):
        db.execute(
            "INSERT INTO client_recovery VALUES (?,?,?,?)",
            (digest(key), binding, self.cipher.encrypt(json.dumps(response).encode()), now + 300),
        )
        db.execute(
            "DELETE FROM client_recovery WHERE key IN "
            "(SELECT key FROM client_recovery ORDER BY expires DESC LIMIT -1 OFFSET 1024)"
        )

    def _pair(self, session_id, subject, generation, absolute, claims, now):
        access_exp, refresh_exp = min(now + 30 * DAY, absolute), min(now + 90 * DAY, absolute)
        common = {"session_id": session_id, "subject": subject, "generation": generation, "route": claims}
        return (
            {
                "token_type": "Bearer",
                "scope": SCOPE,
                "session_id": session_id,
                "generation": generation,
                "access_token": self.codec.encode({**common, "purpose": "access", "exp": access_exp}),
                "refresh_token": self.codec.encode({**common, "purpose": "refresh", "exp": refresh_exp}),
                "access_expires_at": _date(access_exp),
                "refresh_expires_at": _date(refresh_exp),
                "absolute_expires_at": _date(absolute),
            },
            access_exp,
            refresh_exp,
        )

    def issue(
        self, subject: str, client_name: str, operation_key: str, *, now=None, claims=None, approved_at=None
    ) -> dict:
        now, claims = _now(now), claims or {}
        binding = digest(json.dumps(["issue", subject, client_name, claims], sort_keys=True))
        with self._db() as db:
            recovered = self._recover(db, operation_key, binding, now)
            if recovered:
                return recovered
            if db.execute("SELECT 1 FROM client_issuance WHERE key=?", (digest(operation_key),)).fetchone():
                raise ClientAccessError("invalid_grant", 401)
            db.execute("INSERT INTO client_issuance VALUES (?)", (digest(operation_key),))
            approval = now if approved_at is None else approved_at
            if approval > now or approval + 365 * DAY <= now:
                raise ClientAccessError("invalid_grant")
            sid, absolute = secrets.token_hex(16), approval + 365 * DAY
            pair, ae, re = self._pair(sid, subject, 1, absolute, claims, now)
            db.execute(
                "INSERT INTO client_sessions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    sid,
                    subject,
                    client_name,
                    json.dumps(claims),
                    now,
                    absolute,
                    ae,
                    re,
                    1,
                    digest(pair["access_token"]),
                    digest(pair["refresh_token"]),
                    None,
                    None,
                ),
            )
            self._save_recovery(db, operation_key, binding, pair, now)
            return pair

    def _authenticate(self, db, token, purpose, now):
        claims = self.codec.decode(token, purpose=purpose, now=now)
        row = db.execute("SELECT * FROM client_sessions WHERE id=?", (claims["session_id"],)).fetchone()
        if (
            row is None
            or row["revoked"] is not None
            or row["absolute"] <= now
            or row["subject"] != claims["subject"]
            or row[f"{purpose}_exp"] <= now
            or not hmac.compare_digest(row[f"{purpose}_digest"], digest(token))
        ):
            raise ClientAccessError("invalid_grant", 401)
        return row, claims

    def authenticate(self, token: str, *, purpose="access", now=None) -> dict:
        now = _now(now)
        with self._db() as db:
            row, claims = self._authenticate(db, token, purpose, now)
            db.execute("UPDATE client_sessions SET last_used=? WHERE id=?", (now, row["id"]))
            return claims

    def recover_rotation(self, refresh_token: str, idempotency_key: str, *, now=None) -> dict | None:
        """Read only an existing bound recovery; never issue or extend expired credentials."""
        with self._db() as db:
            return self._recover(db, idempotency_key, digest("rotate:" + refresh_token), _now(now))

    def rotate(self, refresh_token: str, idempotency_key: str, *, now=None) -> dict:
        now = _now(now)
        binding = digest("rotate:" + refresh_token)
        with self._db() as db:
            recovered = self._recover(db, idempotency_key, binding, now)
            if recovered:
                return recovered
            row, _ = self._authenticate(db, refresh_token, "refresh", now)
            pair, ae, re = self._pair(
                row["id"], row["subject"], row["generation"] + 1, row["absolute"], json.loads(row["claims"]), now
            )
            db.execute(
                "UPDATE client_sessions SET access_exp=?,refresh_exp=?,generation=?,"
                "access_digest=?,refresh_digest=? WHERE id=?",
                (ae, re, pair["generation"], digest(pair["access_token"]), digest(pair["refresh_token"]), row["id"]),
            )
            self._save_recovery(db, idempotency_key, binding, pair, now)
            return pair

    def revoke(self, token: str, *, now=None):
        now = _now(now)
        with self._db() as db:
            try:
                row, _ = self._authenticate(db, token, "access", now)
            except ClientAccessError:
                row, _ = self._authenticate(db, token, "refresh", now)
            db.execute("UPDATE client_sessions SET revoked=? WHERE id=?", (now, row["id"]))

    def revoke_session(self, subject: str, session_id: str, *, now=None) -> bool:
        with self._db() as db:
            return bool(
                db.execute(
                    "UPDATE client_sessions SET revoked=? WHERE subject=? AND id=?", (_now(now), subject, session_id)
                ).rowcount
            )

    def list_sessions(self, subject: str) -> list[dict]:
        with self._db() as db:
            return [
                {
                    "session_id": r["id"],
                    "generation": r["generation"],
                    "client_name": r["client_name"],
                    "scope": SCOPE,
                    "access_expires_at": _date(r["access_exp"]),
                    "refresh_expires_at": _date(r["refresh_exp"]),
                    "absolute_expires_at": _date(r["absolute"]),
                    "revoked_at": _date(r["revoked"]) if r["revoked"] is not None else None,
                    "last_used_at": _date(r["last_used"]) if r["last_used"] is not None else None,
                }
                for r in db.execute("SELECT * FROM client_sessions WHERE subject=? ORDER BY approved DESC", (subject,))
            ]

    @staticmethod
    def operation_id(subject: str, key: str) -> str:
        if not isinstance(key, str) or not 8 <= len(key) <= 200:
            raise ClientAccessError("invalid_idempotency_key")
        return digest(json.dumps([subject, key]))

    def _operation(self, row):
        return {
            "operation_id": row["key"],
            "status": "pending" if row["status"] is None else "completed" if row["status"] < 400 else "failed",
            "http_status": row["status"],
            "response": json.loads(self.cipher.decrypt(row["response"])) if row["response"] else None,
        }

    def begin_operation(self, subject: str, key: str, method: str, target: str, content: bytes, *, now=None):
        oid = self.operation_id(subject, key)
        binding = digest(json.dumps([method, target, hashlib.sha256(content).hexdigest()]))
        with self._db() as db:
            row = db.execute("SELECT * FROM client_operations WHERE key=?", (oid,)).fetchone()
            if row:
                if not hmac.compare_digest(binding, row["binding"]):
                    raise ClientAccessError("idempotency_conflict", 409)
                return {**self._operation(row), "created": False}
            db.execute("INSERT INTO client_operations VALUES (?,?,NULL,NULL,?)", (oid, binding, _now(now)))
            return {"operation_id": oid, "status": "pending", "http_status": None, "response": None, "created": True}

    def complete_operation(self, subject: str, key: str, status: int, response):
        with self._db() as db:
            db.execute(
                "UPDATE client_operations SET status=?,response=? WHERE key=? AND status IS NULL",
                (status, self.cipher.encrypt(json.dumps(response).encode()), self.operation_id(subject, key)),
            )

    def operation(self, subject: str, key: str):
        with self._db() as db:
            row = db.execute(
                "SELECT * FROM client_operations WHERE key=?", (self.operation_id(subject, key),)
            ).fetchone()
            return self._operation(row) if row else None

    def exchange_entry(self, ticket: str, verifier: str, key: str, entry: dict | None = None, *, now=None):
        """Retain only already PKCE-validated exchanges for crash recovery."""
        now = _now(now)
        if not 8 <= len(key) <= 200:
            raise ClientAccessError("invalid_idempotency_key")
        binding = digest(json.dumps([verifier, key]))
        with self._db() as db:
            db.execute("DELETE FROM client_tickets WHERE expires<=?", (now,))
            row = db.execute("SELECT * FROM client_tickets WHERE key=?", (digest(ticket),)).fetchone()
            if row:
                if not hmac.compare_digest(row["binding"], binding):
                    raise ClientAccessError("invalid_grant")
                return json.loads(self.cipher.decrypt(row["entry"]))
            if entry is None:
                return None
            db.execute(
                "INSERT INTO client_tickets VALUES (?,?,?,?)",
                (digest(ticket), binding, self.cipher.encrypt(json.dumps(entry).encode()), now + 300),
            )
            db.execute(
                "DELETE FROM client_tickets WHERE key IN "
                "(SELECT key FROM client_tickets ORDER BY expires DESC LIMIT -1 OFFSET 256)"
            )
            return entry
