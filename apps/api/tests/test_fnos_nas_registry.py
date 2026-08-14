from __future__ import annotations

import base64
import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from sag_api.core.errors import ConflictError, ValidationError
from sag_api.fnos.nas_registry import REGISTRY_LIMITS, FnOSNasScanRegistry, NasScanEntry


class FakeClock:
    def __init__(self, value: float = 1_000.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


def _ids() -> Iterator[str]:
    index = 0
    while True:
        index += 1
        yield f"id-{index}"


@pytest.fixture
def secret_file(tmp_path: Path) -> Path:
    path = tmp_path / "identity.key"
    path.write_text("a" * 64, encoding="ascii")
    path.chmod(0o600)
    return path


@pytest.fixture
def entry() -> NasScanEntry:
    return NasScanEntry(
        canonical_root="/vol1/Documents",
        canonical_path="/vol1/Documents/Policies/handbook.pdf",
        display_path="Policies/handbook.pdf",
        size_bytes=1234,
        mtime_ns=9876,
        folder_source="host_api",
    )


def _registry(secret_file: Path, clock: FakeClock | None = None) -> FnOSNasScanRegistry:
    values = _ids()
    return FnOSNasScanRegistry(
        secret_file=secret_file,
        clock=clock or FakeClock(),
        id_provider=lambda: next(values),
    )


def _decode_claims(token: str) -> dict[str, object]:
    transport, _signature = token.split(".", 1)
    padding = "=" * (-len(transport) % 4)
    return json.loads(base64.urlsafe_b64decode(transport + padding))


def test_register_returns_opaque_safe_claims_and_resolves_once(
    secret_file: Path, entry: NasScanEntry
) -> None:
    registry = _registry(secret_file)
    scan = registry.register(uid=1000, source_id="source-a", entries=[entry])
    token = scan.selection_tokens[0]
    claims = _decode_claims(token)

    assert set(claims) == {"v", "scan_id", "entry_id", "uid", "source_id", "exp"}
    assert claims == {
        "v": 1,
        "scan_id": "id-1",
        "entry_id": "id-2",
        "uid": 1000,
        "source_id": "source-a",
        "exp": 1900,
    }
    assert scan.scan_id == "id-1"
    assert scan.expires_at == 1900
    assert "/vol" not in token
    assert entry.canonical_path not in token
    assert registry.resolve_many(1000, "source-a", [token]) == [entry]

    with pytest.raises(ConflictError) as consumed:
        registry.resolve_many(1000, "source-a", [token])
    assert consumed.value.code == "nas_selection_expired"


@pytest.mark.parametrize("change", ["signature", "uid", "source", "scan", "entry"])
def test_resolution_rejects_tampering_or_wrong_binding(
    change: str, secret_file: Path, entry: NasScanEntry
) -> None:
    registry = _registry(secret_file)
    token = registry.register(1000, "source-a", [entry]).selection_tokens[0]
    uid = 1000
    source_id = "source-a"
    if change == "signature":
        token = token[:-1] + ("0" if token[-1] != "0" else "1")
    elif change == "uid":
        uid = 1001
    elif change == "source":
        source_id = "source-b"
    else:
        claims = _decode_claims(token)
        claims[f"{change}_id"] = "unknown"
        payload = json.dumps(claims, sort_keys=True, separators=(",", ":")).encode()
        transport = base64.urlsafe_b64encode(payload).decode().rstrip("=")
        token = transport + token[token.index(".") :]

    with pytest.raises(ConflictError) as captured:
        registry.resolve_many(uid, source_id, [token])
    assert captured.value.code == "nas_selection_expired"


def test_expiry_and_fresh_registry_invalidate_selections(
    secret_file: Path, entry: NasScanEntry
) -> None:
    clock = FakeClock()
    registry = _registry(secret_file, clock)
    token = registry.register(1000, "source-a", [entry]).selection_tokens[0]
    clock.value += REGISTRY_LIMITS["ttl_seconds"] + 1

    with pytest.raises(ConflictError):
        registry.resolve_many(1000, "source-a", [token])
    with pytest.raises(ConflictError):
        _registry(secret_file, clock).resolve_many(1000, "source-a", [token])


def test_resolve_rejects_duplicates_and_mixed_scans_without_consuming(
    secret_file: Path, entry: NasScanEntry
) -> None:
    registry = _registry(secret_file)
    first = registry.register(1000, "source-a", [entry])
    second = registry.register(1000, "source-a", [entry])

    with pytest.raises(ValidationError):
        registry.resolve_many(1000, "source-a", [first.selection_tokens[0]] * 2)
    with pytest.raises(ValidationError):
        registry.resolve_many(
            1000,
            "source-a",
            [first.selection_tokens[0], second.selection_tokens[0]],
        )

    assert registry.resolve_many(1000, "source-a", first.selection_tokens) == [entry]


def test_registry_enforces_entry_limit(secret_file: Path, entry: NasScanEntry) -> None:
    registry = _registry(secret_file)
    with pytest.raises(ValidationError, match="扫描结果过多"):
        registry.register(1000, "source-a", [entry] * (REGISTRY_LIMITS["entries_per_scan"] + 1))


def test_registry_evicts_expired_then_oldest_scans(secret_file: Path, entry: NasScanEntry) -> None:
    clock = FakeClock()
    registry = _registry(secret_file, clock)
    scans = []
    for _ in range(REGISTRY_LIMITS["active_scans"]):
        scans.append(registry.register(1000, "source-a", [entry]))
        clock.value += 1

    newest = registry.register(1000, "source-a", [entry])
    with pytest.raises(ConflictError):
        registry.resolve_many(1000, "source-a", scans[0].selection_tokens)
    assert registry.resolve_many(1000, "source-a", newest.selection_tokens) == [entry]

    expired_registry = _registry(secret_file, clock)
    expired = expired_registry.register(1000, "source-a", [entry])
    clock.value += REGISTRY_LIMITS["ttl_seconds"] + 1
    fresh = [expired_registry.register(1000, "source-a", [entry]) for _ in range(4)]
    with pytest.raises(ConflictError):
        expired_registry.resolve_many(1000, "source-a", expired.selection_tokens)
    assert expired_registry.resolve_many(1000, "source-a", fresh[0].selection_tokens) == [entry]


def test_discard_invalidates_a_scan(secret_file: Path, entry: NasScanEntry) -> None:
    registry = _registry(secret_file)
    scan = registry.register(1000, "source-a", [entry])
    registry.discard(scan.scan_id)
    registry.discard(scan.scan_id)

    with pytest.raises(ConflictError):
        registry.resolve_many(1000, "source-a", scan.selection_tokens)
