from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import UTC, datetime

import jwt


class DecisionTokenError(ValueError):
    pass


def _require_canonical_jwt(token: str) -> None:
    parts = token.split(".")
    if len(parts) != 3:
        raise DecisionTokenError("invalid OCTX decision token encoding")
    for part in parts:
        try:
            decoded = base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))
        except (ValueError, TypeError) as error:
            raise DecisionTokenError("invalid OCTX decision token encoding") from error
        canonical = base64.urlsafe_b64encode(decoded).rstrip(b"=").decode("ascii")
        if canonical != part:
            raise DecisionTokenError("non-canonical OCTX decision token encoding")


@dataclass(frozen=True, slots=True)
class DecisionTokenClaims:
    transfer_id: str
    asset_id: str
    source_revisions: dict[str, int]
    highest_version: str | None
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class ExportDecisionTokenClaims:
    transfer_id: str
    source_id: str
    selected_document_ids: tuple[str, ...]
    selected_article_ids: tuple[str, ...]
    selection_fingerprint: str
    source_revision: int
    nonce: str
    expires_at: datetime


def issue_decision_token(claims: DecisionTokenClaims, *, secret: str) -> str:
    expires = claims.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=UTC)
    payload = {
        "typ": "octx-import-decision",
        "transfer_id": claims.transfer_id,
        "asset_id": claims.asset_id,
        "source_revisions": claims.source_revisions,
        "highest_version": claims.highest_version,
        "exp": expires,
    }
    return jwt.encode(payload, secret, algorithm="HS256")


def verify_decision_token(token: str, *, secret: str) -> DecisionTokenClaims:
    try:
        _require_canonical_jwt(token)
        payload = jwt.decode(token, secret, algorithms=["HS256"])
        if payload.get("typ") != "octx-import-decision":
            raise DecisionTokenError("invalid OCTX decision token type")
        revisions = payload["source_revisions"]
        if not isinstance(revisions, dict) or not all(
            isinstance(key, str) and isinstance(value, int) for key, value in revisions.items()
        ):
            raise DecisionTokenError("invalid OCTX decision source revisions")
        return DecisionTokenClaims(
            transfer_id=str(payload["transfer_id"]),
            asset_id=str(payload["asset_id"]),
            source_revisions=dict(revisions),
            highest_version=(str(payload["highest_version"]) if payload.get("highest_version") is not None else None),
            expires_at=datetime.fromtimestamp(int(payload["exp"]), tz=UTC),
        )
    except DecisionTokenError:
        raise
    except (jwt.InvalidTokenError, KeyError, TypeError, ValueError) as error:
        raise DecisionTokenError("invalid or expired OCTX decision token") from error


def issue_export_decision_token(claims: ExportDecisionTokenClaims, *, secret: str) -> str:
    expires = claims.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=UTC)
    return jwt.encode(
        {
            "typ": "octx-export-decision",
            "transfer_id": claims.transfer_id,
            "source_id": claims.source_id,
            "selected_document_ids": list(claims.selected_document_ids),
            "selected_article_ids": list(claims.selected_article_ids),
            "selection_fingerprint": claims.selection_fingerprint,
            "source_revision": claims.source_revision,
            "nonce": claims.nonce,
            "exp": expires,
        },
        secret,
        algorithm="HS256",
    )


def verify_export_decision_token(token: str, *, secret: str) -> ExportDecisionTokenClaims:
    try:
        _require_canonical_jwt(token)
        payload = jwt.decode(token, secret, algorithms=["HS256"])
        if payload.get("typ") != "octx-export-decision":
            raise DecisionTokenError("invalid OCTX export decision token type")
        document_ids = payload["selected_document_ids"]
        article_ids = payload["selected_article_ids"]
        if not isinstance(document_ids, list) or not all(isinstance(value, str) for value in document_ids):
            raise DecisionTokenError("invalid OCTX export document selection")
        if not isinstance(article_ids, list) or not all(isinstance(value, str) for value in article_ids):
            raise DecisionTokenError("invalid OCTX export article selection")
        return ExportDecisionTokenClaims(
            transfer_id=str(payload["transfer_id"]),
            source_id=str(payload["source_id"]),
            selected_document_ids=tuple(document_ids),
            selected_article_ids=tuple(article_ids),
            selection_fingerprint=str(payload["selection_fingerprint"]),
            source_revision=int(payload["source_revision"]),
            nonce=str(payload["nonce"]),
            expires_at=datetime.fromtimestamp(int(payload["exp"]), tz=UTC),
        )
    except DecisionTokenError:
        raise
    except (jwt.InvalidTokenError, KeyError, TypeError, ValueError) as error:
        raise DecisionTokenError("invalid or expired OCTX export decision token") from error
