"""Persist local DeepSeek Harness connector credentials and source selection."""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from sag_api.core.errors import NotFoundError
from sag_api.db.models import Setting, Source, User

_SCOPE = "global"
_KEY = "dsh_integration"
_STATE_WRITE_LOCKS: dict[asyncio.AbstractEventLoop, asyncio.Lock] = {}


class _ConnectionFileSettings(Protocol):
    dsh_connection_file: str | None


@dataclass(frozen=True)
class DshIntegrationState:
    """The credential and optional default source exposed to a local connector."""

    token: str
    default_source_id: str | None


async def _load_row(session: AsyncSession) -> Setting | None:
    statement = (
        select(Setting)
        .where(Setting.scope == _SCOPE, Setting.key == _KEY)
        .execution_options(populate_existing=True)
    )
    return await session.scalar(statement)


def _state_from_row(row: Setting) -> DshIntegrationState:
    return DshIntegrationState(
        token=str(row.value["token"]),
        default_source_id=row.value.get("default_source_id"),
    )


def _new_token() -> str:
    return f"sag_local_{secrets.token_urlsafe(32)}"


def connection_file_path(active_settings: _ConnectionFileSettings) -> Path:
    """Return the configured or platform-default local DSH descriptor path."""
    if active_settings.dsh_connection_file:
        return Path(active_settings.dsh_connection_file).expanduser()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "SAG" / "dsh-connection.json"
    if sys.platform == "win32":
        config_home = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
        return config_home / "SAG" / "dsh-connection.json"
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return config_home / "sag" / "dsh-connection.json"


def _state_write_lock() -> asyncio.Lock:
    """Return the local API process's lock for connector-state writes."""
    loop = asyncio.get_running_loop()
    lock = _STATE_WRITE_LOCKS.get(loop)
    if lock is None:
        lock = asyncio.Lock()
        _STATE_WRITE_LOCKS[loop] = lock
    return lock


def _is_state_unique_violation(error: IntegrityError) -> bool:
    original = error.orig
    constraint_name = getattr(getattr(original, "diag", None), "constraint_name", None)
    return constraint_name == "uq_setting_scope_key" or "UNIQUE constraint failed: settings.scope, settings.key" in str(
        original
    )


async def _get_or_create_row(session: AsyncSession) -> Setting:
    row = await _load_row(session)
    if row is None:
        row = Setting(
            scope=_SCOPE,
            key=_KEY,
            value={"token": _new_token(), "default_source_id": None},
        )
        session.add(row)
        try:
            await session.commit()
        except IntegrityError as error:
            await session.rollback()
            if not _is_state_unique_violation(error):
                raise
            row = await _load_row(session)
            if row is None:
                raise
    return row


async def get_or_create_state(session: AsyncSession) -> DshIntegrationState:
    """Return the persisted connector state, creating it for a new local installation."""
    row = await _load_row(session)
    if row is None:
        async with _state_write_lock():
            row = await _get_or_create_row(session)
    return _state_from_row(row)


async def authenticate_connector(session: AsyncSession, token: str) -> User | None:
    """Return the first active user when the supplied connector token matches."""
    state = await get_or_create_state(session)
    if not secrets.compare_digest(state.token, token):
        return None
    return await session.scalar(
        select(User)
        .where(User.is_active.is_(True))
        .order_by(User.created_at.asc(), User.id.asc())
        .limit(1)
    )


async def update_default_source(
    session: AsyncSession,
    source_id: str | None,
) -> DshIntegrationState:
    """Persist a selected source after confirming that it exists, or clear it."""
    if source_id is not None and await session.get(Source, source_id) is None:
        raise NotFoundError("信源不存在")

    async with _state_write_lock():
        row = await _get_or_create_row(session)
        state = _state_from_row(row)
        row.value = {"token": state.token, "default_source_id": source_id}
        await session.commit()
        return _state_from_row(row)


async def regenerate_token(session: AsyncSession) -> DshIntegrationState:
    """Replace the connector token while retaining the selected default source."""
    async with _state_write_lock():
        row = await _get_or_create_row(session)
        state = _state_from_row(row)
        row.value = {"token": _new_token(), "default_source_id": state.default_source_id}
        await session.commit()
        return _state_from_row(row)


def _replace_connection_file(target: Path, payload: dict[str, object]) -> None:
    """Replace one descriptor file without exposing a partially written payload."""
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=target.parent,
        prefix=f".{target.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            if os.name != "nt":
                os.fchmod(file.fileno(), 0o600)
            json.dump(payload, file, ensure_ascii=False, indent=2)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, target)
    except BaseException:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


def _connection_payload(state: DshIntegrationState) -> dict[str, object]:
    from sag_api.core.config import settings

    return {
        "schemaVersion": 1,
        "name": "SAG 知识库",
        "apiUrl": f"{settings.dsh_public_url}/api/v1",
        "mcpUrl": f"{settings.dsh_public_url}/mcp/",
        "accessToken": state.token,
        "defaultSourceId": state.default_source_id,
    }


def _publish_connection_state(state: DshIntegrationState) -> Path:
    from sag_api.core.config import settings

    target = connection_file_path(settings)
    _replace_connection_file(target, _connection_payload(state))
    return target


async def _commit_and_publish_state(
    session: AsyncSession,
    row: Setting,
    state: DshIntegrationState,
) -> DshIntegrationState:
    """Commit and publish one state change, compensating an atomic file-write failure."""
    previous = _state_from_row(row)
    row.value = {
        "token": state.token,
        "default_source_id": state.default_source_id,
    }
    await session.commit()
    try:
        _publish_connection_state(state)
    except OSError as publication_error:
        row.value = {
            "token": previous.token,
            "default_source_id": previous.default_source_id,
        }
        try:
            await session.commit()
        except Exception as restoration_error:
            raise ExceptionGroup(
                "DSH connection publication and state restoration failed",
                [publication_error, restoration_error],
            ) from publication_error
        raise
    return state


async def update_default_source_and_publish(
    session: AsyncSession,
    source_id: str | None,
) -> DshIntegrationState:
    """Publish a source selection, restoring the previous state if publication fails."""
    if source_id is not None and await session.get(Source, source_id) is None:
        raise NotFoundError("信源不存在")

    async with _state_write_lock():
        row = await _get_or_create_row(session)
        current = _state_from_row(row)
        return await _commit_and_publish_state(
            session,
            row,
            DshIntegrationState(token=current.token, default_source_id=source_id),
        )


async def regenerate_token_and_publish(session: AsyncSession) -> DshIntegrationState:
    """Publish a replacement token, restoring the previous token if publication fails."""
    async with _state_write_lock():
        row = await _get_or_create_row(session)
        current = _state_from_row(row)
        return await _commit_and_publish_state(
            session,
            row,
            DshIntegrationState(
                token=_new_token(),
                default_source_id=current.default_source_id,
            ),
        )


async def write_connection_file(session: AsyncSession) -> Path:
    """Atomically publish the current local connector descriptor for discovery."""
    async with _state_write_lock():
        state = _state_from_row(await _get_or_create_row(session))
        return _publish_connection_state(state)
