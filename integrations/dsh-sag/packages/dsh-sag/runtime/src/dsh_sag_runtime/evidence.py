"""Opaque, deterministic evidence references shared by search and read."""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class EvidenceLocator:
    namespace_id: str
    source_id: str
    chunk_id: str


class EvidenceRefCodec:
    """Encode locators and enforce namespace admission while decoding."""

    def __init__(self, namespaces: set[str] | frozenset[str]) -> None:
        self._namespaces = frozenset(namespaces)

    def encode(self, locator: EvidenceLocator) -> str:
        if locator.namespace_id not in self._namespaces:
            raise ValueError("evidence namespace is not configured")
        raw = json.dumps(
            {"c": locator.chunk_id, "n": locator.namespace_id, "s": locator.source_id, "v": 1},
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")

    def encode_from_parts(self, namespace_id: str, source_id: str, chunk_id: str) -> str:
        """Encode one locator without exposing its wire keys to callers."""
        return self.encode(EvidenceLocator(namespace_id, source_id, chunk_id))

    def decode(self, value: str) -> EvidenceLocator:
        try:
            if not value or any(character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_" for character in value):
                raise ValueError
            padded = value + "=" * (-len(value) % 4)
            raw = base64.b64decode(padded, altchars=b"-_", validate=True)
            payload = json.loads(raw)
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("invalid evidence reference") from error
        if not isinstance(payload, dict) or set(payload) != {"v", "n", "s", "c"} or payload.get("v") != 1:
            raise ValueError("invalid evidence reference")
        if not all(isinstance(payload.get(key), str) and payload[key] for key in ("n", "s", "c")):
            raise ValueError("invalid evidence reference")
        if payload["n"] not in self._namespaces:
            raise ValueError("evidence namespace is not configured")
        return EvidenceLocator(payload["n"], payload["s"], payload["c"])
