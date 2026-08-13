from __future__ import annotations

import json
import os
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any


def _namespace(value: uuid.UUID | str) -> uuid.UUID:
    try:
        return value if isinstance(value, uuid.UUID) else uuid.UUID(value)
    except (AttributeError, TypeError, ValueError) as error:
        raise ValueError("installation namespace must be a UUID") from error


def _identity(value: str) -> str:
    try:
        return str(uuid.UUID(value))
    except (AttributeError, TypeError, ValueError) as error:
        raise ValueError(f"OCTX UUID is invalid: {value!r}") from error


def installation_local_id(
    namespace: uuid.UUID | str, record_kind: str, octx_id: str
) -> str:
    """Map one exchange identity into an installation-local UUIDv5."""
    kind = record_kind.strip().casefold()
    if not kind:
        raise ValueError("record kind must not be empty")
    return str(uuid.uuid5(_namespace(namespace), f"{kind}:{_identity(octx_id)}"))


def relation_local_id(
    namespace: uuid.UUID | str,
    relation_kind: str,
    *octx_ids: str,
) -> str:
    """Create a deterministic local ID for an OCTX relation tuple."""
    kind = relation_kind.strip().casefold()
    if not kind:
        raise ValueError("relation kind must not be empty")
    identities: Sequence[str] = tuple(_identity(value) for value in octx_ids)
    if len(identities) < 2:
        raise ValueError("relation identity requires at least two OCTX UUIDs")
    return str(uuid.uuid5(_namespace(namespace), f"{kind}:{':'.join(identities)}"))


def named_local_id(
    namespace: uuid.UUID | str, record_kind: str, stable_name: str
) -> str:
    """Map a normalized, non-OCTX identity such as an EntityType into UUIDv5."""
    kind = record_kind.strip().casefold()
    name = stable_name.strip()
    if not kind:
        raise ValueError("record kind must not be empty")
    if not name:
        raise ValueError("stable name must not be empty")
    return str(uuid.uuid5(_namespace(namespace), f"{kind}:{name}"))


def _octx_uuid(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = uuid.UUID(value)
    except ValueError:
        return None
    return str(parsed) if parsed.version == 7 else None


class ProducerIdMap:
    """Persistent producer state for stable IDs that are not SAG UUIDv4 values."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._dirty = False
        if self.path.is_file():
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if payload.get("version") != 1 or not isinstance(payload.get("mappings"), dict):
                raise ValueError("unsupported OCTX producer ID map")
            self._mappings = {
                str(key): str(value) for key, value in payload["mappings"].items()
            }
        else:
            self._mappings: dict[str, str] = {}

    def __enter__(self) -> ProducerIdMap:
        return self

    def __exit__(self, exc_type: object, *_args: object) -> None:
        if exc_type is None:
            self.flush()

    def exchange_id(
        self,
        record_kind: str,
        local_id: str,
        *,
        extra_data: dict[str, Any] | None = None,
    ) -> str:
        kind = record_kind.strip().casefold()
        if not kind:
            raise ValueError("record kind must not be empty")
        key = f"{kind}:{local_id}"
        namespace = (extra_data or {}).get("octx")
        preserved = _octx_uuid(
            namespace.get("record_id") if isinstance(namespace, dict) else None
        )
        if preserved is not None:
            if self._mappings.get(key) != preserved:
                self._mappings[key] = preserved
                self._dirty = True
            return preserved
        try:
            parsed = uuid.UUID(local_id)
        except ValueError:
            parsed = None
        if parsed is not None and parsed.version == 7:
            return str(parsed)
        if parsed is not None and parsed.version == 4:
            value = str(parsed)
            return value[:14] + "7" + value[15:]
        existing = self._mappings.get(key)
        if existing is not None:
            return existing
        generated = str(uuid.uuid4())
        generated = generated[:14] + "7" + generated[15:]
        self._mappings[key] = generated
        self._dirty = True
        return generated

    def flush(self) -> None:
        if not self._dirty:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}.tmp")
        payload = {"version": 1, "mappings": dict(sorted(self._mappings.items()))}
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        os.chmod(temporary, 0o600)
        temporary.replace(self.path)
        self._dirty = False
