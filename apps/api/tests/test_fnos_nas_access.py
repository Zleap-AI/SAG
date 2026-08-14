from __future__ import annotations

import os
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from sag_api.core.error_taxonomy import ErrorCode
from sag_api.core.errors import ConflictError, ValidationError
from sag_api.db.models import FnOSNasLegacyFolder
from sag_api.fnos.identity import GatewayIdentity, derive_fnos_internal_key
from sag_api.fnos.open_api import (
    ConvertedPath,
    FnOSOpenAPIError,
    PlatformConfig,
    SharedFolder,
    UserACL,
)
from sag_api.services.fnos_nas_access import FnOSNasAccessService, NasMode, decide_mode


class FakeOpenAPI:
    def __init__(self, version: str = "1.2.0500") -> None:
        self.version = version
        self.paths = ["/vol1/Documents", "/vol2/Private"]
        self.readable = {"/vol1/Documents": True, "/vol2/Private": False}
        self.platform_error: FnOSOpenAPIError | None = None
        self.shared_error: FnOSOpenAPIError | None = None
        self.calls: list[str] = []

    async def get_platform_config(self) -> PlatformConfig:
        self.calls.append("platform")
        if self.platform_error is not None:
            raise self.platform_error
        return PlatformConfig(system_version=self.version, system_language="zh-CN")

    async def get_shared_accessible_folders(self) -> list[SharedFolder]:
        self.calls.append("shared")
        if self.shared_error is not None:
            raise self.shared_error
        return [SharedFolder(path=path) for path in self.paths]

    async def check_user_acl(self, uid: int, paths: list[str]) -> list[UserACL]:
        self.calls.append(f"acl:{uid}")
        return [
            UserACL(path=path, readable=self.readable.get(path, False), writable=False, deletable=False)
            for path in paths
        ]

    async def convert_path(self, path: str | list[str], language: str) -> list[ConvertedPath]:
        self.calls.append(f"convert:{language}")
        paths = [path] if isinstance(path, str) else path
        return [ConvertedPath(path=item, semantic_path=f"NAS/{Path(item).name}") for item in paths]


def _host_error(code: ErrorCode, status: int) -> FnOSOpenAPIError:
    return FnOSOpenAPIError("safe host error", code=code, status_code=status, retryable=False)


@pytest.mark.parametrize(
    ("version", "probe_ok", "expected"),
    [
        ("1.2.0499", True, NasMode.LEGACY_MANUAL),
        ("1.2.0500", True, NasMode.AUTOMATIC),
        ("1.2.0501-beta.1", True, NasMode.AUTOMATIC),
        ("2.0.0", True, NasMode.AUTOMATIC),
        ("garbage", True, NasMode.LEGACY_MANUAL),
        ("", True, NasMode.LEGACY_MANUAL),
        ("1.2.0500", False, NasMode.LEGACY_MANUAL),
    ],
)
def test_decide_mode_uses_conservative_product_threshold(
    version: str, probe_ok: bool, expected: NasMode
) -> None:
    assert decide_mode(version, probe_ok=probe_ok) is expected


@pytest.fixture
async def nas_session() -> AsyncSession:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(FnOSNasLegacyFolder.__table__.create)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session:
        yield session
    await engine.dispose()


@pytest.fixture
def secret_file(tmp_path: Path) -> Path:
    path = tmp_path / "identity.key"
    path.write_text("a" * 64, encoding="ascii")
    path.chmod(0o600)
    return path


def test_derived_keys_are_domain_separated_and_validate_secret(secret_file: Path) -> None:
    folder_key = derive_fnos_internal_key(secret_file, b"sag-fnos-nas-folder-v1")
    token_key = derive_fnos_internal_key(secret_file, b"sag-fnos-nas-token-v1")
    assert len(folder_key) == 32
    assert folder_key != token_key
    with pytest.raises(ValueError):
        derive_fnos_internal_key(secret_file, b"")


@pytest.mark.asyncio
async def test_non_admin_status_never_queries_or_returns_host_folders(
    nas_session: AsyncSession, secret_file: Path
) -> None:
    host = FakeOpenAPI()
    service = FnOSNasAccessService(host, secret_file=secret_file)

    status = await service.status(
        nas_session,
        GatewayIdentity(uid=1000, username="Alice", is_admin=False),
        "zh-CN",
    )

    assert status.eligible is False
    assert status.mode is NasMode.UNAVAILABLE
    assert status.folders == []
    assert status.reason == "administrator_required"
    assert host.calls == []


@pytest.mark.asyncio
async def test_automatic_status_filters_acl_converts_display_and_resolves_opaque_id(
    nas_session: AsyncSession, secret_file: Path
) -> None:
    host = FakeOpenAPI()
    service = FnOSNasAccessService(host, secret_file=secret_file)
    identity = GatewayIdentity(uid=1000, username="Alice", is_admin=True)

    status = await service.status(nas_session, identity, "zh-CN")

    assert status.mode is NasMode.AUTOMATIC
    assert status.system_version == "1.2.0500"
    assert len(status.folders) == 1
    folder = status.folders[0]
    assert folder.display_path == "NAS/Documents"
    assert folder.readable is True
    assert "/vol" not in folder.id
    assert "/vol" not in repr(folder)
    resolved = await service.resolve_root(nas_session, identity, folder.id)
    assert resolved.path == Path("/vol1/Documents")
    assert resolved.authorized_path == "/vol1/Documents"
    assert resolved.display_path == "NAS/Documents"

    with pytest.raises(ConflictError):
        await service.resolve_root(nas_session, identity, folder.id[:-1] + "x")

    host.paths = []
    with pytest.raises(ConflictError) as revoked:
        await service.revalidate_root(nas_session, identity, resolved)
    assert revoked.value.code == "nas_folder_revoked"


@pytest.mark.asyncio
async def test_old_or_malformed_versions_do_not_probe_shared_folders(
    nas_session: AsyncSession, secret_file: Path
) -> None:
    for version in ("1.2.0499", "garbage", ""):
        host = FakeOpenAPI(version)
        service = FnOSNasAccessService(host, secret_file=secret_file)
        status = await service.status(
            nas_session,
            GatewayIdentity(uid=1000, username="Alice", is_admin=True),
            "zh-CN",
        )
        assert status.mode is NasMode.LEGACY_MANUAL
        assert "shared" not in host.calls


@pytest.mark.asyncio
async def test_unavailable_host_capabilities_fall_back_to_legacy(
    nas_session: AsyncSession, secret_file: Path
) -> None:
    identity = GatewayIdentity(uid=1000, username="Alice", is_admin=True)
    host = FakeOpenAPI()
    host.shared_error = _host_error(ErrorCode.NAS_HOST_API_NOT_FOUND, 404)
    status = await FnOSNasAccessService(host, secret_file=secret_file).status(
        nas_session, identity, "zh-CN"
    )
    assert status.mode is NasMode.LEGACY_MANUAL

    for code in (ErrorCode.NAS_HOST_AUTH_EXPIRED, ErrorCode.NAS_SCOPE_MISSING):
        host = FakeOpenAPI()
        host.shared_error = _host_error(code, 503)
        status = await FnOSNasAccessService(host, secret_file=secret_file).status(
            nas_session, identity, "zh-CN"
        )
        assert status.mode is NasMode.LEGACY_MANUAL
        assert status.reason == "host_authorization_unavailable"


@pytest.mark.asyncio
async def test_platform_api_unavailable_falls_back_with_actionable_reason(
    nas_session: AsyncSession, secret_file: Path
) -> None:
    identity = GatewayIdentity(uid=1000, username="Alice", is_admin=True)
    host = FakeOpenAPI()
    host.platform_error = _host_error(ErrorCode.NAS_HOST_API_NOT_FOUND, 404)
    status = await FnOSNasAccessService(host, secret_file=secret_file).status(
        nas_session, identity, "zh-CN"
    )
    assert status.mode is NasMode.LEGACY_MANUAL
    assert status.system_version is None

    host = FakeOpenAPI()
    host.platform_error = _host_error(ErrorCode.NAS_HOST_AUTH_EXPIRED, 503)
    status = await FnOSNasAccessService(host, secret_file=secret_file).status(
        nas_session, identity, "zh-CN"
    )
    assert status.mode is NasMode.LEGACY_MANUAL
    assert status.reason == "host_authorization_unavailable"


@pytest.mark.asyncio
async def test_legacy_folder_registration_is_safe_idempotent_and_deletable(
    tmp_path: Path, nas_session: AsyncSession, secret_file: Path
) -> None:
    (tmp_path / "vol1" / "Documents").mkdir(parents=True)
    host = FakeOpenAPI("1.2.0499")
    service = FnOSNasAccessService(host, secret_file=secret_file, legacy_mount_root=tmp_path)

    first = await service.register_legacy_folder(nas_session, "/vol1/Documents")
    second = await service.register_legacy_folder(nas_session, "/vol1/Documents")
    assert first == second
    assert first.source == "legacy_manual"
    assert first.display_path == "/vol1/Documents"
    assert "/vol1" not in first.id

    identity = GatewayIdentity(uid=1000, username="Alice", is_admin=True)
    root = await service.resolve_root(nas_session, identity, first.id)
    assert root.path == tmp_path / "vol1" / "Documents"
    assert root.authorized_path == "/vol1/Documents"
    await service.revalidate_root(nas_session, identity, root)
    await service.delete_legacy_folder(nas_session, first.id)
    with pytest.raises(ConflictError):
        await service.resolve_root(nas_session, identity, first.id)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        "relative/Documents",
        "/vol0/Documents",
        "/vol1/@appdata/sag",
        "/vol1/runtime/sag",
        "/vol1/Docs/../Documents",
        "/vol1/Documents/",
    ],
)
async def test_legacy_registration_rejects_unsafe_lexical_paths(
    path: str, tmp_path: Path, nas_session: AsyncSession, secret_file: Path
) -> None:
    (tmp_path / "vol1" / "Documents").mkdir(parents=True)
    service = FnOSNasAccessService(FakeOpenAPI("1.2.0499"), secret_file=secret_file, legacy_mount_root=tmp_path)
    with pytest.raises(ValidationError):
        await service.register_legacy_folder(nas_session, path)


@pytest.mark.asyncio
async def test_legacy_registration_classifies_volume_root_as_invalid(
    tmp_path: Path, nas_session: AsyncSession, secret_file: Path
) -> None:
    (tmp_path / "vol1").mkdir()
    service = FnOSNasAccessService(
        FakeOpenAPI("1.2.0499"), secret_file=secret_file, legacy_mount_root=tmp_path
    )

    with pytest.raises(ValidationError) as captured:
        await service.register_legacy_folder(nas_session, "/vol1")

    assert captured.value.code == ErrorCode.NAS_FOLDER_PATH_INVALID


@pytest.mark.asyncio
async def test_legacy_registration_rejects_symlink_file_missing_and_unreadable(
    tmp_path: Path,
    nas_session: AsyncSession,
    secret_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    volume = tmp_path / "vol1"
    volume.mkdir()
    (volume / "real").mkdir()
    (volume / "link").symlink_to(volume / "real", target_is_directory=True)
    (volume / "file.pdf").write_bytes(b"pdf")
    (volume / "unreadable").mkdir()
    service = FnOSNasAccessService(FakeOpenAPI("1.2.0499"), secret_file=secret_file, legacy_mount_root=tmp_path)

    for path in ("/vol1/link", "/vol1/file.pdf", "/vol1/missing"):
        with pytest.raises(ValidationError):
            await service.register_legacy_folder(nas_session, path)

    real_access = os.access
    monkeypatch.setattr(
        os,
        "access",
        lambda candidate, mode: False if Path(candidate).name == "unreadable" else real_access(candidate, mode),
    )
    with pytest.raises(ValidationError) as captured:
        await service.register_legacy_folder(nas_session, "/vol1/unreadable")
    assert captured.value.code == ErrorCode.NAS_FOLDER_UNREADABLE
