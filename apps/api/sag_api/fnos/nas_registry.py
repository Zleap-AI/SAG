"""Bounded, worker-local storage for opaque fnOS NAS scan selections."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from collections import OrderedDict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Literal

from sag_api.core.error_taxonomy import ErrorCode
from sag_api.core.errors import ConflictError, ValidationError
from sag_api.fnos.identity import derive_fnos_internal_key

REGISTRY_LIMITS = {
    "active_scans": 4,
    "entries_per_scan": 5_000,
    "ttl_seconds": 15 * 60,
}
_SELECTION_PURPOSE = b"sag-fnos-nas-selection-v1"


@dataclass(frozen=True)
class NasScanEntry:
    canonical_root: str
    canonical_path: str
    display_path: str
    size_bytes: int
    mtime_ns: int
    folder_source: Literal["host_api", "legacy_manual"]


@dataclass(frozen=True)
class RegisteredScan:
    scan_id: str
    selection_tokens: tuple[str, ...]
    expires_at: int


@dataclass(frozen=True)
class _Manifest:
    uid: int
    source_id: str
    expires_at: int
    entries: dict[str, NasScanEntry]


class FnOSNasScanRegistry:
    def __init__(
        self,
        *,
        secret_file: Path,
        clock: Callable[[], float] = time.time,
        id_provider: Callable[[], str] = lambda: secrets.token_urlsafe(18),
    ) -> None:
        self._key = derive_fnos_internal_key(secret_file, _SELECTION_PURPOSE)
        self._clock = clock
        self._id_provider = id_provider
        self._manifests: OrderedDict[str, _Manifest] = OrderedDict()
        self._lock = Lock()

    def register(
        self,
        uid: int,
        source_id: str,
        entries: Sequence[NasScanEntry],
    ) -> RegisteredScan:
        if type(uid) is not int or uid < 1 or not isinstance(source_id, str) or not source_id:
            raise ValidationError("扫描归属信息无效")
        if len(entries) > REGISTRY_LIMITS["entries_per_scan"]:
            raise ValidationError("扫描结果过多，请缩小目录范围")
        if not all(isinstance(entry, NasScanEntry) for entry in entries):
            raise ValidationError("扫描结果无效")

        now = self._clock()
        expires_at = int(now) + REGISTRY_LIMITS["ttl_seconds"]
        with self._lock:
            self._evict_expired(now)
            while len(self._manifests) >= REGISTRY_LIMITS["active_scans"]:
                self._manifests.popitem(last=False)
            scan_id = self._unique_id(set(self._manifests))
            entry_map: dict[str, NasScanEntry] = {}
            tokens: list[str] = []
            for entry in entries:
                entry_id = self._unique_id(set(entry_map))
                entry_map[entry_id] = entry
                tokens.append(
                    self._encode(
                        {
                            "v": 1,
                            "scan_id": scan_id,
                            "entry_id": entry_id,
                            "uid": uid,
                            "source_id": source_id,
                            "exp": expires_at,
                        }
                    )
                )
            self._manifests[scan_id] = _Manifest(
                uid=uid,
                source_id=source_id,
                expires_at=expires_at,
                entries=entry_map,
            )
        return RegisteredScan(scan_id=scan_id, selection_tokens=tuple(tokens), expires_at=expires_at)

    def resolve_many(
        self,
        uid: int,
        source_id: str,
        tokens: Sequence[str],
    ) -> list[NasScanEntry]:
        if not tokens:
            raise ValidationError("请至少选择一个文件")
        if len(tokens) != len(set(tokens)):
            raise ValidationError("文件选择不能重复")

        claims = [self._decode(token) for token in tokens]
        scan_ids = {claim["scan_id"] for claim in claims}
        if len(scan_ids) != 1:
            raise ValidationError("一次导入只能提交同一次扫描的文件")
        scan_id = next(iter(scan_ids))
        now = self._clock()
        with self._lock:
            self._evict_expired(now)
            manifest = self._manifests.get(scan_id)
            if manifest is None or manifest.uid != uid or manifest.source_id != source_id:
                raise _selection_expired()
            if manifest.expires_at <= now:
                del self._manifests[scan_id]
                raise _selection_expired()

            resolved: list[NasScanEntry] = []
            for claim in claims:
                if (
                    claim["uid"] != uid
                    or claim["source_id"] != source_id
                    or claim["exp"] != manifest.expires_at
                    or claim["exp"] <= now
                ):
                    raise _selection_expired()
                entry = manifest.entries.get(claim["entry_id"])
                if entry is None:
                    raise _selection_expired()
                resolved.append(entry)
            del self._manifests[scan_id]
            return list(resolved)

    def discard(self, scan_id: str) -> None:
        with self._lock:
            self._manifests.pop(scan_id, None)

    def _evict_expired(self, now: float) -> None:
        for scan_id in [
            scan_id
            for scan_id, manifest in self._manifests.items()
            if manifest.expires_at <= now
        ]:
            del self._manifests[scan_id]

    def _unique_id(self, existing: set[str]) -> str:
        for _ in range(32):
            value = self._id_provider()
            if isinstance(value, str) and value and "." not in value and value not in existing:
                return value
        raise RuntimeError("unable to generate a unique NAS scan identifier")

    def _encode(self, claims: dict[str, object]) -> str:
        payload = json.dumps(claims, sort_keys=True, separators=(",", ":")).encode()
        transport = base64.urlsafe_b64encode(payload).decode().rstrip("=")
        signature = hmac.new(self._key, payload, hashlib.sha256).hexdigest()
        return f"{transport}.{signature}"

    def _decode(self, token: str) -> dict[str, object]:
        try:
            if not isinstance(token, str) or len(token) > 2048:
                raise ValueError
            transport, signature = token.split(".")
            if len(signature) != 64:
                raise ValueError
            padding = "=" * (-len(transport) % 4)
            payload = base64.b64decode(transport + padding, altchars=b"-_", validate=True)
            expected = hmac.new(self._key, payload, hashlib.sha256).hexdigest()
            if not hmac.compare_digest(signature, expected):
                raise ValueError
            claims = json.loads(payload)
            if not isinstance(claims, dict) or set(claims) != {
                "v",
                "scan_id",
                "entry_id",
                "uid",
                "source_id",
                "exp",
            }:
                raise ValueError
            if (
                claims["v"] != 1
                or type(claims["uid"]) is not int
                or claims["uid"] < 1
                or type(claims["exp"]) is not int
                or not isinstance(claims["scan_id"], str)
                or not claims["scan_id"]
                or not isinstance(claims["entry_id"], str)
                or not claims["entry_id"]
                or not isinstance(claims["source_id"], str)
                or not claims["source_id"]
            ):
                raise ValueError
            return claims
        except (UnicodeError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            raise _selection_expired() from None


def _selection_expired() -> ConflictError:
    return ConflictError("文件选择已失效，请重新扫描", code=ErrorCode.NAS_SELECTION_EXPIRED)
