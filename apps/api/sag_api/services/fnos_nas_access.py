"""Authorization policy and safe root resolution for fnOS NAS imports."""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import stat
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sag_api.core.error_taxonomy import ErrorCode
from sag_api.core.errors import ConflictError, ValidationError
from sag_api.db.models import FnOSNasLegacyFolder
from sag_api.fnos.identity import GatewayIdentity, derive_fnos_internal_key
from sag_api.fnos.open_api import FnOSOpenAPIClient, FnOSOpenAPIError

_VERSION_RE = re.compile(r"^(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)(?:[-+].*)?$")
_LEGACY_PATH_RE = re.compile(r"^/vol[1-9][0-9]*(?:/[^/]+)+$")
_FORBIDDEN_LEGACY_COMPONENTS = frozenset({"@appdata", "@apphome", "@apptemp", "runtime"})
_AUTOMATIC_VERSION = (1, 2, 500)
_FOLDER_PURPOSE = b"sag-fnos-nas-folder-v1"


class NasMode(StrEnum):
    AUTOMATIC = "automatic"
    LEGACY_MANUAL = "legacy_manual"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class NasFolder:
    id: str
    display_path: str
    source: str
    readable: bool


@dataclass(frozen=True)
class NasStatus:
    eligible: bool
    mode: NasMode
    system_version: str | None
    automatic_authorization: bool
    folders: list[NasFolder]
    reason: str | None = None


@dataclass(frozen=True)
class ResolvedNasRoot:
    path: Path
    authorized_path: str
    display_path: str
    source: str
    folder_id: str


def decide_mode(system_version: str, *, probe_ok: bool) -> NasMode:
    match = _VERSION_RE.match(system_version.strip()) if isinstance(system_version, str) else None
    if match is None:
        return NasMode.LEGACY_MANUAL
    version = tuple(int(match.group(part)) for part in ("major", "minor", "patch"))
    if version < _AUTOMATIC_VERSION or not probe_ok:
        return NasMode.LEGACY_MANUAL
    return NasMode.AUTOMATIC


class FnOSNasAccessService:
    def __init__(
        self,
        open_api: FnOSOpenAPIClient,
        *,
        secret_file: Path,
        legacy_mount_root: Path = Path("/"),
    ) -> None:
        self._open_api = open_api
        self._folder_key = derive_fnos_internal_key(secret_file, _FOLDER_PURPOSE)
        self._legacy_mount_root = legacy_mount_root.resolve()

    async def status(self, session: AsyncSession, identity: GatewayIdentity, language: str) -> NasStatus:
        if not identity.is_admin:
            return NasStatus(
                eligible=False,
                mode=NasMode.UNAVAILABLE,
                system_version=None,
                automatic_authorization=False,
                folders=[],
                reason="administrator_required",
            )

        try:
            platform = await self._open_api.get_platform_config()
        except FnOSOpenAPIError as error:
            if error.code == ErrorCode.NAS_HOST_API_NOT_FOUND:
                return await self._legacy_status(session, "")
            if error.code in {ErrorCode.NAS_HOST_AUTH_EXPIRED, ErrorCode.NAS_SCOPE_MISSING}:
                return await self._legacy_status(
                    session,
                    "",
                    reason="host_authorization_unavailable",
                )
            raise
        preliminary = decide_mode(platform.system_version, probe_ok=True)
        if preliminary is NasMode.LEGACY_MANUAL:
            return await self._legacy_status(session, platform.system_version)
        try:
            folders = await self._automatic_folders(identity, language)
        except FnOSOpenAPIError as error:
            if error.code == ErrorCode.NAS_HOST_API_NOT_FOUND:
                return await self._legacy_status(session, platform.system_version)
            if error.code in {ErrorCode.NAS_HOST_AUTH_EXPIRED, ErrorCode.NAS_SCOPE_MISSING}:
                return await self._legacy_status(
                    session,
                    platform.system_version,
                    reason="host_authorization_unavailable",
                )
            raise
        return NasStatus(
            eligible=True,
            mode=NasMode.AUTOMATIC,
            system_version=platform.system_version,
            automatic_authorization=True,
            folders=folders,
        )

    async def register_legacy_folder(self, session: AsyncSession, path: str) -> NasFolder:
        self._validate_legacy_path(path)
        existing = await session.scalar(select(FnOSNasLegacyFolder).where(FnOSNasLegacyFolder.path == path))
        if existing is None:
            existing = FnOSNasLegacyFolder(path=path, display_label=path)
            session.add(existing)
            await session.commit()
            await session.refresh(existing)
        return self._legacy_folder(existing)

    async def delete_legacy_folder(self, session: AsyncSession, folder_id: str) -> None:
        record = await self._find_legacy_record(session, folder_id)
        if record is None:
            raise _revoked()
        await session.delete(record)
        await session.commit()

    async def resolve_root(
        self, session: AsyncSession, identity: GatewayIdentity, folder_id: str
    ) -> ResolvedNasRoot:
        if folder_id.startswith("l."):
            record = await self._find_legacy_record(session, folder_id)
            if record is None:
                raise _revoked()
            physical = self._validate_legacy_path(record.path)
            return ResolvedNasRoot(
                path=physical,
                authorized_path=record.path,
                display_path=record.display_label,
                source="legacy_manual",
                folder_id=folder_id,
            )

        automatic = await self._open_api.get_shared_accessible_folders()
        for shared in automatic:
            if hmac.compare_digest(self._automatic_folder_id(shared.path), folder_id):
                acl = await self._open_api.check_user_acl(identity.uid, [shared.path])
                if not acl or not acl[0].readable:
                    raise _revoked()
                converted = await self._open_api.convert_path(shared.path, "zh-CN")
                display = converted[0].semantic_path if converted else Path(shared.path).name
                return ResolvedNasRoot(
                    path=Path(shared.path),
                    authorized_path=shared.path,
                    display_path=display,
                    source="host_api",
                    folder_id=folder_id,
                )

        raise _revoked()

    async def revalidate_root(
        self, session: AsyncSession, identity: GatewayIdentity, root: ResolvedNasRoot
    ) -> None:
        if root.source == "legacy_manual":
            record = (
                await self._find_legacy_record(session, root.folder_id)
                if root.folder_id
                else await session.scalar(
                    select(FnOSNasLegacyFolder).where(FnOSNasLegacyFolder.path == root.authorized_path)
                )
            )
            if record is None or record.path != root.authorized_path:
                raise _revoked()
            try:
                physical = self._validate_legacy_path(record.path)
            except ValidationError:
                raise _revoked() from None
            if physical != root.path:
                raise _revoked()
            return

        shared = await self._open_api.get_shared_accessible_folders()
        if not any(
            item.path == root.authorized_path
            and (
                not root.folder_id
                or hmac.compare_digest(self._automatic_folder_id(item.path), root.folder_id)
            )
            for item in shared
        ):
            raise _revoked()
        acl = await self._open_api.check_user_acl(identity.uid, [root.authorized_path])
        if not acl or not acl[0].readable:
            raise _revoked()

    async def _automatic_folders(self, identity: GatewayIdentity, language: str) -> list[NasFolder]:
        shared = await self._open_api.get_shared_accessible_folders()
        if not shared:
            return []
        paths = [item.path for item in shared]
        acl = {item.path: item for item in await self._open_api.check_user_acl(identity.uid, paths)}
        readable_paths = [path for path in paths if acl.get(path) is not None and acl[path].readable]
        if not readable_paths:
            return []
        converted = {
            item.path: item.semantic_path
            for item in await self._open_api.convert_path(readable_paths, language)
        }
        return [
            NasFolder(
                id=self._automatic_folder_id(path),
                display_path=converted.get(path, Path(path).name),
                source="host_api",
                readable=True,
            )
            for path in readable_paths
        ]

    async def _legacy_status(
        self,
        session: AsyncSession,
        version: str,
        *,
        reason: str | None = None,
    ) -> NasStatus:
        records = (await session.scalars(select(FnOSNasLegacyFolder).order_by(FnOSNasLegacyFolder.id))).all()
        folders: list[NasFolder] = []
        for record in records:
            try:
                self._validate_legacy_path(record.path)
            except ValidationError:
                continue
            folders.append(self._legacy_folder(record))
        return NasStatus(
            eligible=True,
            mode=NasMode.LEGACY_MANUAL,
            system_version=version or None,
            automatic_authorization=False,
            folders=folders,
            reason=reason,
        )

    def _automatic_folder_id(self, path: str) -> str:
        return "a." + hmac.new(self._folder_key, b"automatic\0" + path.encode(), hashlib.sha256).hexdigest()

    def _legacy_folder(self, record: FnOSNasLegacyFolder) -> NasFolder:
        return NasFolder(
            id=self._legacy_folder_id(record.id),
            display_path=record.display_label,
            source="legacy_manual",
            readable=True,
        )

    def _legacy_folder_id(self, record_id: str) -> str:
        signature = hmac.new(self._folder_key, b"legacy\0" + record_id.encode(), hashlib.sha256).hexdigest()
        return f"l.{record_id}.{signature}"

    async def _find_legacy_record(
        self, session: AsyncSession, folder_id: str
    ) -> FnOSNasLegacyFolder | None:
        parts = folder_id.split(".")
        if len(parts) != 3 or parts[0] != "l" or not parts[1]:
            return None
        expected = self._legacy_folder_id(parts[1])
        if not hmac.compare_digest(expected, folder_id):
            return None
        return await session.get(FnOSNasLegacyFolder, parts[1])

    def _validate_legacy_path(self, path: str) -> Path:
        if not isinstance(path, str) or not _LEGACY_PATH_RE.fullmatch(path):
            raise ValidationError(
                "授权目录路径无效",
                code=ErrorCode.NAS_FOLDER_PATH_INVALID,
            )
        pure = PurePosixPath(path)
        if str(pure) != path or any(part in {".", ".."} for part in pure.parts):
            raise ValidationError("授权目录路径无效", code=ErrorCode.NAS_FOLDER_PATH_INVALID)
        if any(part.casefold() in _FORBIDDEN_LEGACY_COMPONENTS for part in pure.parts):
            raise ValidationError(
                "该系统目录不能用于导入",
                code=ErrorCode.NAS_FOLDER_PATH_INVALID,
            )
        physical = self._legacy_mount_root.joinpath(*pure.parts[1:])
        try:
            metadata = os.lstat(physical)
        except OSError as error:
            raise ValidationError(
                "授权目录不存在或不可读",
                code=ErrorCode.NAS_FOLDER_UNREADABLE,
            ) from error
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise ValidationError(
                "授权路径必须是非链接目录",
                code=ErrorCode.NAS_FOLDER_PATH_INVALID,
            )
        try:
            resolved = physical.resolve(strict=True)
            if resolved != physical.absolute() or os.path.commonpath((resolved, self._legacy_mount_root)) != str(
                self._legacy_mount_root
            ):
                raise ValidationError(
                    "授权目录路径无效",
                    code=ErrorCode.NAS_FOLDER_PATH_INVALID,
                )
        except (OSError, ValueError) as error:
            raise ValidationError(
                "授权目录路径无效",
                code=ErrorCode.NAS_FOLDER_PATH_INVALID,
            ) from error
        if not os.access(physical, os.R_OK | os.X_OK):
            raise ValidationError(
                "授权目录不可读",
                code=ErrorCode.NAS_FOLDER_UNREADABLE,
            )
        return physical


def _revoked() -> ConflictError:
    return ConflictError("NAS 目录授权已失效", code=ErrorCode.NAS_FOLDER_REVOKED)
