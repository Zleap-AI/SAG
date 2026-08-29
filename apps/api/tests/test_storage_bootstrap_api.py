from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import sag_api.db.models  # noqa: F401
from sag_api.core.config import Settings
from sag_api.db.base import Base
from sag_api.db.models import User
from sag_api.main import create_app
from sag_api.upgrades.contracts import StorageBootstrapPhase, StorageChoice, UpgradeReport
from sag_api.upgrades.coordinator import StorageBootstrapCoordinator
from sag_api.upgrades.state import BootstrapState, BootstrapStateStore

FIXTURE = Path(__file__).parent / "fixtures" / "zleap_sag_071" / "fixture.zip"


def _fingerprint(path: Path) -> str:
    digest = hashlib.sha256()
    for item in sorted(path.rglob("*")):
        if item.is_file():
            digest.update(str(item.relative_to(path)).encode())
            digest.update(item.read_bytes())
    return digest.hexdigest()


async def _fixture(tmp_path: Path):
    engine_dir = tmp_path / "engine"
    engine_dir.mkdir()
    with zipfile.ZipFile(FIXTURE) as archive:
        archive.extractall(engine_dir)
    database = tmp_path / "app.db"
    db_engine = create_async_engine(f"sqlite+aiosqlite:///{database}")
    async with db_engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(db_engine, expire_on_commit=False)
    settings = Settings(
        data_dir=str(engine_dir),
        database_url=f"sqlite+aiosqlite:///{database}",
        llm_api_key="fixture",
        embedding_api_key="fixture",
        embedding_dimensions=12,
        _env_file=None,
    )
    return engine_dir, db_engine, sessions, settings


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", ["{", '{"schema_version": 999}'])
async def test_malformed_bootstrap_journal_keeps_liveness_and_is_preserved(
    tmp_path: Path, payload: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sag_api import main as main_module

    _engine_dir, db_engine, sessions, test_settings = await _fixture(tmp_path)
    journal = tmp_path / ".storage-upgrades" / "bootstrap.json"
    journal.parent.mkdir(parents=True)
    journal.write_text(payload, encoding="utf-8")

    async def noop(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(main_module, "settings", test_settings)
    monkeypatch.setattr(main_module, "SessionLocal", sessions)
    monkeypatch.setattr(
        "sag_api.upgrades.integration.StorageBootstrapCoordinator",
        StorageBootstrapCoordinator,
    )
    monkeypatch.setattr(main_module, "init_db", noop)
    monkeypatch.setattr(main_module, "dispose_db", noop)
    monkeypatch.setattr(
        "sag_api.services.settings_service.apply_startup_overrides",
        noop,
    )
    app = main_module.create_app()
    try:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                health = await client.get("/api/v1/system/health")
                bootstrap = await client.get("/api/v1/system/storage-bootstrap")

            assert health.status_code == 200
            assert health.json() == {"status": "ok"}
            assert bootstrap.status_code == 200
            assert bootstrap.json() == {
                "phase": "failed",
                "detected_version": None,
                "target_version": "0.8.2",
                "choices": [],
                "stage": "bootstrap_state",
                "error": "storage bootstrap state is invalid",
                "recoverable": True,
                "runtime_ready": False,
            }
            assert journal.read_text(encoding="utf-8") == payload
    finally:
        await db_engine.dispose()


@pytest.mark.asyncio
async def test_inspect_requires_choice_without_touching_legacy(tmp_path: Path) -> None:
    engine_dir, db_engine, sessions, settings = await _fixture(tmp_path)
    before = _fingerprint(engine_dir)
    try:
        coordinator = StorageBootstrapCoordinator(settings, sessions)
        assert (await coordinator.inspect()).phase == StorageBootstrapPhase.CHOICE_REQUIRED
        assert _fingerprint(engine_dir) == before
        payload = json.loads((tmp_path / ".storage-upgrades" / "bootstrap.json").read_text())
        assert payload["schema_version"] == 1
        assert payload["phase"] == "choice_required"
    finally:
        await db_engine.dispose()


@pytest.mark.asyncio
async def test_choice_is_authenticated_gated_and_idempotent(tmp_path: Path, monkeypatch) -> None:
    engine_dir, db_engine, sessions, settings = await _fixture(tmp_path)
    async with sessions() as session:
        user = User(email="owner@example.test", name="Owner", password_hash="hash")
        session.add(user)
        await session.commit()
        await session.refresh(user)
    coordinator = StorageBootstrapCoordinator(settings, sessions)
    await coordinator.inspect()
    app = create_app()
    app.state.storage_bootstrap = coordinator
    from sag_api.core import db as db_module
    from sag_api.core.config import settings as runtime_settings
    monkeypatch.setattr(db_module, "SessionLocal", sessions)
    monkeypatch.setattr(runtime_settings, "auth_mode", "password")
    monkeypatch.setattr(runtime_settings, "allow_registration", True)
    token = __import__("sag_api.core.security", fromlist=["create_access_token"]).create_access_token(user.id)
    headers = {"Authorization": f"Bearer {token}"}
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            public = await client.get("/api/v1/system/storage-bootstrap")
            assert public.status_code == 200
            registered = await client.post(
                "/api/v1/auth/register",
                json={"email": "upgrade@example.test", "password": "StrongPassword123"},
            )
            assert registered.status_code == 201
            auth_status = await client.get("/api/v1/auth/status")
            assert auth_status.status_code == 200
            assert "preserved_path" not in public.json()
            assert "diagnostic_path" not in public.json()
            assert "accepted_choice" not in public.json()
            authenticated = await client.get(
                "/api/v1/system/storage-bootstrap", headers=headers
            )
            assert authenticated.status_code == 200
            assert authenticated.json()["preserved_path"] == str(engine_dir)
            assert authenticated.json()["diagnostic_path"].endswith("bootstrap.json")

            anonymous = await client.post(
                "/api/v1/system/storage-bootstrap/choice", json={"choice": "fresh"}
            )
            assert anonymous.status_code == 401
            blocked = await client.get("/api/v1/sources", headers=headers)
            assert blocked.status_code == 503
            assert blocked.json()["error"]["code"] == "storage_upgrade_required"
            choice_url = "/api/v1/system/storage-bootstrap/choice"
            first = await client.post(choice_url, headers=headers, json={"choice": "fresh"})
            second = await client.post(choice_url, headers=headers, json={"choice": "fresh"})
            assert first.status_code == second.status_code == 202
            public_processing = await client.get("/api/v1/system/storage-bootstrap")
            authenticated_processing = await client.get(
                "/api/v1/system/storage-bootstrap", headers=headers
            )
            assert "accepted_choice" not in public_processing.json()
            assert public_processing.json()["choices"] == []
            assert authenticated_processing.json()["choices"] == []
            assert authenticated_processing.json()["accepted_choice"] == "fresh"
            assert coordinator.started_tasks == 1
            conflict = await client.post(choice_url, headers=headers, json={"choice": "migrate"})
            assert conflict.status_code == 409
    finally:
        await coordinator.wait()
        await db_engine.dispose()


@pytest.mark.asyncio
async def test_maintenance_login_requires_exact_existing_name_without_side_effects(
    tmp_path: Path, monkeypatch
) -> None:
    _engine_dir, db_engine, sessions, settings = await _fixture(tmp_path)
    async with sessions() as session:
        owner = User(email="owner@example.test", name="Owner", password_hash="hash")
        session.add(owner)
        await session.commit()
        await session.refresh(owner)
        owner_id = owner.id

    coordinator = StorageBootstrapCoordinator(settings, sessions)
    await coordinator.inspect()
    app = create_app()
    app.state.storage_bootstrap = coordinator
    from sag_api.core import db as db_module
    from sag_api.core.config import settings as runtime_settings

    monkeypatch.setattr(db_module, "SessionLocal", sessions)
    monkeypatch.setattr(runtime_settings, "auth_mode", "local")
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            correct = await client.post("/api/v1/auth/login", json={"name": "Owner"})
            wrong = await client.post("/api/v1/auth/login", json={"name": "Missing"})
            assert correct.status_code == 200
            assert correct.json()["user"]["id"] == owner_id
            assert wrong.status_code == 401

        async with sessions() as session:
            users = list((await session.execute(select(User))).scalars())
            assert [(user.id, user.name) for user in users] == [(owner_id, "Owner")]
    finally:
        await db_engine.dispose()


@pytest.mark.asyncio
async def test_processing_restart_resumes_and_callback_failure_is_retryable(
    tmp_path: Path, monkeypatch
) -> None:
    _engine_dir, db_engine, sessions, settings = await _fixture(tmp_path)
    calls: list[str] = []
    BootstrapStateStore(tmp_path / ".storage-upgrades" / "bootstrap.json").save(
        BootstrapState(
            phase=StorageBootstrapPhase.PROCESSING,
            source_version="legacy_0_7",
            target_version="0.8.2",
            choice=StorageChoice.MIGRATE,
            actor_user_id="owner",
            adapter_id="fixture",
            stage="processing",
        )
    )

    class Adapter:
        migration_id = "fixture"

        async def migrate(self, _context):
            return UpgradeReport("migrated")

    async def on_ready():
        calls.append("ready")
        raise RuntimeError("runtime not installed")

    monkeypatch.setattr(
        "sag_api.upgrades.coordinator.select_adapter", lambda *_args, **_kwargs: Adapter()
    )
    coordinator = StorageBootstrapCoordinator(settings, sessions, on_ready=on_ready)
    assert (await coordinator.inspect()).phase is StorageBootstrapPhase.PROCESSING
    await coordinator.wait()
    assert coordinator.store.load().phase is StorageBootstrapPhase.FAILED

    assert (
        await coordinator.choose(StorageChoice.MIGRATE, "owner")
    ).phase is StorageBootstrapPhase.PROCESSING
    await coordinator.wait()
    assert calls == ["ready", "ready"]
    await db_engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    (
        "/api/v1/system/health-details",
        "/api/v1/auth/me-extra",
        "/api/v1/auth/me/profile",
        "/api/v1/auth/register-extra",
        "/api/v1/auth/status/details",
        "/mcp",
        "/api/v1/openai/models",
        "/docs",
        "/",
    ),
)
async def test_maintenance_allowlist_rejects_suffixes_and_nested_paths(
    tmp_path: Path, path: str
) -> None:
    _engine_dir, db_engine, sessions, settings = await _fixture(tmp_path)
    coordinator = StorageBootstrapCoordinator(settings, sessions)
    await coordinator.inspect()
    app = create_app()
    app.state.storage_bootstrap = coordinator
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(path)
    assert response.status_code == 503
    await db_engine.dispose()
