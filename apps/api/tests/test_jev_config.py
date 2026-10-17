"""JEV settings, credential rotation and draft protocol checks without network calls."""

import json
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import delete, select

from sag_api.core.config import Settings, settings
from sag_api.schemas.system import ModelConfigUpdate


def test_jev_environment_is_normalized_and_injected(monkeypatch):
    from sag_api.sag.config_builder import build_engine_config

    monkeypatch.setenv("SAG_JEV_ENDPOINT", "  https://jev.example/score  ")
    monkeypatch.setenv("SAG_JEV_MODEL", "  scoring-model  ")
    monkeypatch.setenv("SAG_JEV_API_KEY", "synthetic-jev-key")
    monkeypatch.setenv("SAG_JEV_RANKING_TIMEOUT_SECONDS", "12.5")
    monkeypatch.setenv("SAG_JEV_FAILURE_POLICY", "fallback_to_score")
    configured = Settings(_env_file=None)
    assert configured.jev_endpoint == "https://jev.example/score"
    assert configured.jev_model == "scoring-model"
    assert configured.jev_ranking_timeout_seconds == 12.5
    assert configured.jev_failure_policy == "fallback_to_score"
    config = build_engine_config(configured)
    assert config.jev is not None
    assert config.jev.endpoint == "https://jev.example/score"
    assert config.jev.model == "scoring-model"
    assert config.jev.api_key == "synthetic-jev-key"


def test_blank_jev_endpoint_disables_engine_client():
    from sag_api.sag.config_builder import build_engine_config

    configured = Settings(_env_file=None, jev_endpoint="   ")
    assert configured.jev_endpoint is None
    assert build_engine_config(configured).jev is None


@pytest.mark.parametrize(
    "patch",
    [
        {"jev_endpoint": "jev.example/score"},
        {"jev_endpoint": "ftp://jev.example/score"},
        {"jev_endpoint": "https:///score"},
        {"jev_endpoint": "https://user:password@jev.example/score"},
        {"jev_endpoint": "https://jev.example/score#fragment"},
        {"jev_endpoint": "https://jev.example:bad/score"},
        {"jev_model": "   "},
        {"jev_model": None},
        {"jev_ranking_timeout_seconds": 0},
        {"jev_ranking_timeout_seconds": 121},
        {"jev_ranking_timeout_seconds": None},
        {"jev_failure_policy": "vector"},
        {"jev_failure_policy": None},
    ],
)
def test_jev_rejects_invalid_environment_and_patch_values(patch):
    with pytest.raises(ValueError):
        Settings(_env_file=None, **patch)
    with pytest.raises(ValueError):
        ModelConfigUpdate.model_validate(patch)


def test_jev_patch_normalizes_blank_endpoint_and_model():
    patch = ModelConfigUpdate(jev_endpoint="  ", jev_model=" custom-model ")
    assert patch.model_dump(exclude_unset=True) == {
        "jev_endpoint": None,
        "jev_model": "custom-model",
    }


@pytest.mark.parametrize(
    ("overrides", "expected_endpoint", "expected_key"),
    [
        ({"jev_endpoint": "https://saved.example/score"}, "https://saved.example/score", None),
        ({"jev_endpoint": "https://environment.example/score"}, "https://environment.example/score", "environment-key"),
        (
            {"jev_endpoint": "https://saved.example/score", "jev_api_key": "saved-key"},
            "https://saved.example/score", "saved-key",
        ),
        ({"jev_endpoint": "https://saved.example/score", "jev_api_key": None}, "https://saved.example/score", None),
        ({"jev_endpoint": None}, None, None),
        ({"jev_model": "saved-model"}, "https://environment.example/score", "environment-key"),
    ],
)
def test_saved_jev_endpoint_never_inherits_a_different_environment_endpoints_key(
    overrides, expected_endpoint, expected_key,
):
    from sag_api.services.settings_service import apply_overrides

    configured = Settings(
        _env_file=None, jev_endpoint="https://environment.example/score", jev_api_key="environment-key",
    )
    apply_overrides(configured, overrides)
    assert configured.jev_endpoint == expected_endpoint
    assert configured.jev_api_key == expected_key


@pytest.fixture
async def jev_config_client(monkeypatch):
    from sag_api.core.db import SessionLocal
    from sag_api.db.models import Setting
    from sag_api.main import app
    from sag_api.services import settings_service

    async def skip_unrelated_mcp(_factory, _app):
        return None

    monkeypatch.setattr("sag_api.runtime._RuntimeFactory.start_mcp", skip_unrelated_mcp)
    snapshot = settings.model_dump()
    source_snapshot = dict(settings_service._MODEL_CONFIG_SOURCES)
    try:
        async with app.router.lifespan_context(app):
            async with SessionLocal() as session:
                await session.execute(delete(Setting).where(Setting.scope == "global", Setting.key == "model_config"))
                await session.commit()
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
                registration = await client.post(
                    "/api/v1/auth/register",
                    json={"email": f"jev-{uuid4().hex}@t.com", "password": "password123"},
                )
                assert registration.status_code == 201
                headers = {"Authorization": f"Bearer {registration.json()['access_token']}"}
                resets = []
                original = app.state.knowledge_runtime.apply_settings

                async def capture_apply(updated, *, reset_engines):
                    resets.append(reset_engines)
                    return await original(updated, reset_engines=reset_engines)

                monkeypatch.setattr(app.state.knowledge_runtime, "apply_settings", capture_apply)
                yield client, headers, resets
    finally:
        async with SessionLocal() as session:
            await session.execute(delete(Setting).where(Setting.scope == "global", Setting.key == "model_config"))
            await session.commit()
        for key, value in snapshot.items():
            setattr(settings, key, value)
        settings_service._MODEL_CONFIG_SOURCES.clear()
        settings_service._MODEL_CONFIG_SOURCES.update(source_snapshot)


async def test_jev_config_persists_masks_restores_and_rebuilds_only_connection_changes(jev_config_client):
    from sag_api.core.db import SessionLocal
    from sag_api.db.models import Setting
    from sag_api.services.settings_service import apply_startup_overrides

    client, headers, resets = jev_config_client
    response = await client.put(
        "/api/v1/system/model-config", headers=headers,
        json={"jev_endpoint": "https://jev.example/score", "jev_api_key": "synthetic-saved-key"},
    )
    assert response.status_code == 200, response.text
    config = response.json()["config"]
    assert config["jev_endpoint"] == "https://jev.example/score"
    assert config["jev_model"] == "jev-latest"
    assert config["jev_api_key_set"] is True
    assert "jev_api_key" not in config and "synthetic-saved-key" not in response.text
    assert config["sources"]["jev_endpoint"] == "database"
    assert "jev_api_key" not in config["sources"]
    assert resets == [True]

    response = await client.put(
        "/api/v1/system/model-config", headers=headers,
        json={"jev_ranking_timeout_seconds": 18, "jev_failure_policy": "fallback_to_score", "jev_api_key": " "},
    )
    assert response.status_code == 200, response.text
    assert settings.jev_api_key == "synthetic-saved-key"
    assert resets[-1] is False
    async with SessionLocal() as session:
        row = await session.scalar(select(Setting).where(Setting.scope == "global", Setting.key == "model_config"))
        assert row.value["jev_ranking_timeout_seconds"] == 18
        assert row.value["jev_api_key"] == "synthetic-saved-key"
    settings.jev_endpoint = None
    settings.jev_api_key = None
    settings.jev_ranking_timeout_seconds = 30
    await apply_startup_overrides(SessionLocal)
    assert settings.jev_endpoint == "https://jev.example/score"
    assert settings.jev_api_key == "synthetic-saved-key"
    assert settings.jev_ranking_timeout_seconds == 18

    await client.put(
        "/api/v1/system/model-config", headers=headers, json={"jev_api_key": "synthetic-rotated-key"},
    )
    assert settings.jev_api_key == "synthetic-rotated-key"
    assert resets[-1] is True
    await client.put("/api/v1/system/model-config", headers=headers, json={"jev_model": "other-model"})
    assert resets[-1] is True
    response = await client.put(
        "/api/v1/system/model-config", headers=headers, json={"jev_endpoint": "https://other.example/score"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["config"]["jev_api_key_set"] is False
    assert not settings.jev_api_key
    assert resets[-1] is True
    await client.put(
        "/api/v1/system/model-config", headers=headers,
        json={"jev_endpoint": "https://third.example/score", "jev_api_key": "replacement-key"},
    )
    assert settings.jev_api_key == "replacement-key"
    assert resets[-1] is True
    await client.put("/api/v1/system/model-config", headers=headers, json={"jev_endpoint": None})
    assert settings.jev_endpoint is None and not settings.jev_api_key
    assert resets[-1] is True


async def test_jev_service_rejects_invalid_full_candidate_before_persisting(jev_config_client):
    from sag_api.core.db import SessionLocal
    from sag_api.core.errors import ConfigurationError
    from sag_api.services.settings_service import save_model_config

    client, headers, resets = jev_config_client
    before = (await client.get("/api/v1/system/model-config", headers=headers)).json()
    async with SessionLocal() as session:
        with pytest.raises(ConfigurationError):
            await save_model_config(session, {"jev_endpoint": "file:///secret", "jev_ranking_timeout_seconds": 0})
    assert (await client.get("/api/v1/system/model-config", headers=headers)).json() == before
    assert resets == []


async def test_same_endpoint_save_without_key_cannot_reuse_a_new_environments_key_after_restart(jev_config_client):
    from sag_api.core.db import SessionLocal
    from sag_api.db.models import Setting
    from sag_api.services.settings_service import apply_startup_overrides

    client, headers, _resets = jev_config_client
    settings.jev_endpoint = "https://saved.example/score"
    settings.jev_api_key = "first-environment-key"
    response = await client.put(
        "/api/v1/system/model-config", headers=headers,
        json={"jev_endpoint": "https://saved.example/score", "jev_api_key": ""},
    )
    assert response.status_code == 200, response.text
    assert settings.jev_api_key == "first-environment-key"
    async with SessionLocal() as session:
        row = await session.scalar(select(Setting).where(Setting.scope == "global", Setting.key == "model_config"))
        assert row.value["jev_endpoint"] == "https://saved.example/score"
        assert "jev_api_key" not in row.value

    # A new deployment supplies a different endpoint/key pair; the database still wins
    # on endpoint, but must not borrow that new environment's secret.
    settings.jev_endpoint = "https://new-environment.example/score"
    settings.jev_api_key = "new-environment-key"
    await apply_startup_overrides(SessionLocal)
    assert settings.jev_endpoint == "https://saved.example/score"
    assert settings.jev_api_key is None
    config = (await client.get("/api/v1/system/model-config", headers=headers)).json()
    assert config["jev_api_key_set"] is False


@pytest.mark.parametrize("initial_endpoint", ["https://saved.example/score", None])
async def test_key_only_save_binds_credential_to_current_endpoint_after_environment_change(
    jev_config_client, initial_endpoint,
):
    from sag_api.core.db import SessionLocal
    from sag_api.db.models import Setting
    from sag_api.sag.config_builder import build_engine_config
    from sag_api.services.settings_service import apply_startup_overrides

    client, headers, _resets = jev_config_client
    settings.jev_endpoint = initial_endpoint
    settings.jev_api_key = None
    response = await client.put(
        "/api/v1/system/model-config", headers=headers, json={"jev_api_key": "saved-credential"},
    )
    assert response.status_code == 200, response.text
    async with SessionLocal() as session:
        row = await session.scalar(select(Setting).where(Setting.scope == "global", Setting.key == "model_config"))
        assert row.value["jev_endpoint"] == initial_endpoint
        assert row.value["jev_api_key"] == "saved-credential"

    settings.jev_endpoint = "https://new-environment.example/score"
    settings.jev_api_key = "new-environment-key"
    await apply_startup_overrides(SessionLocal)
    assert settings.jev_endpoint == initial_endpoint
    assert settings.jev_api_key == "saved-credential"
    config = build_engine_config(settings)
    if initial_endpoint is None:
        assert config.jev is None
    else:
        assert config.jev.endpoint == initial_endpoint
        assert config.jev.api_key == "saved-credential"


class JevHttpTransport:
    """Only replace outbound HTTP; the published client's protocol validation stays real."""

    def __init__(self):
        self.calls = []
        self.status = 200
        self.malformed = False

    def session(self, **_kwargs):
        owner = self

        class Session:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            def post(self, endpoint, *, data, headers, allow_redirects):
                payload = json.loads(data)
                owner.calls.append((endpoint, headers, payload, allow_redirects))

                class Response:
                    status = owner.status
                    headers = {}

                    async def __aenter__(self):
                        return self

                    async def __aexit__(self, *_args):
                        return None

                    async def json(self, **_kwargs):
                        if owner.malformed:
                            return {"answers": {}}
                        return {
                            "model": payload["model"],
                            "usage": {"input_tokens": 8, "output_tokens": 2},
                            "answers": {key: {"type": "noul", "noul": 0.8} for key in payload["questions"]},
                        }

                return Response()

        return Session()


async def test_jev_draft_checks_protocol_without_mutating_saved_configuration(jev_config_client, monkeypatch):
    from sag_api.core.db import SessionLocal
    from sag_api.db.models import Setting
    from sag_api.main import app

    client, headers, resets = jev_config_client
    await client.put(
        "/api/v1/system/model-config", headers=headers,
        json={"jev_endpoint": "https://saved.example/score", "jev_api_key": "synthetic-saved-key"},
    )
    before = settings.model_dump()
    runtime_before = app.state.knowledge_runtime._settings.model_dump()
    reset_count = len(resets)
    transport = JevHttpTransport()
    monkeypatch.setattr("zleap.sag.core.ai.jev.aiohttp.ClientSession", transport.session)

    response = await client.post(
        "/api/v1/system/model-config/jev/test", headers=headers,
        json={"jev_model": "draft-model", "jev_api_key": ""},
    )
    assert response.status_code == 200, response.text
    assert response.json()["ok"] is True
    endpoint, sent_headers, payload, redirects = transport.calls[-1]
    assert endpoint == "https://saved.example/score"
    assert sent_headers["Authorization"] == "Bearer synthetic-saved-key"
    assert redirects is False
    assert payload["model"] == "draft-model"
    assert set(payload) == {"model", "state", "questions"}
    assert len(payload["questions"]) == 2
    assert all(question["type"] == "noul" for question in payload["questions"].values())
    assert "synthetic-saved-key" not in response.text

    response = await client.post(
        "/api/v1/system/model-config/jev/test", headers=headers,
        json={"jev_endpoint": "https://draft.example/score", "jev_api_key": ""},
    )
    assert response.json()["ok"] is True  # An unauthenticated gateway is supported.
    assert transport.calls[-1][0] == "https://draft.example/score"
    assert transport.calls[-1][1]["Authorization"] == "Bearer "
    assert settings.model_dump() == before
    assert app.state.knowledge_runtime._settings.model_dump() == runtime_before
    assert len(resets) == reset_count
    async with SessionLocal() as session:
        row = await session.scalar(select(Setting).where(Setting.scope == "global", Setting.key == "model_config"))
        assert row.value["jev_endpoint"] == "https://saved.example/score"
        assert row.value["jev_api_key"] == "synthetic-saved-key"


@pytest.mark.parametrize("failure", ["auth", "malformed", "timeout"])
async def test_jev_draft_failure_is_bounded_and_does_not_expose_credentials(jev_config_client, monkeypatch, failure):
    client, headers, _resets = jev_config_client
    transport = JevHttpTransport()
    transport.status = 401 if failure == "auth" else 200
    transport.malformed = failure == "malformed"
    monkeypatch.setattr("zleap.sag.core.ai.jev.aiohttp.ClientSession", transport.session)
    if failure == "timeout":
        def time_out(**_kwargs):
            raise TimeoutError("synthetic-sensitive-provider-body")

        monkeypatch.setattr("zleap.sag.core.ai.jev.aiohttp.ClientSession", time_out)
    response = await client.post(
        "/api/v1/system/model-config/jev/test", headers=headers,
        json={"jev_endpoint": "https://draft.example/score", "jev_api_key": "synthetic-draft-key"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["ok"] is False
    assert response.json()["message"]
    assert "synthetic-draft-key" not in response.text
    assert "synthetic-sensitive-provider-body" not in response.text


async def test_jev_draft_without_endpoint_fails_without_network(jev_config_client, monkeypatch):
    client, headers, _resets = jev_config_client

    def forbidden_transport(**_kwargs):
        pytest.fail("Missing JEV endpoint must be rejected before HTTP")

    monkeypatch.setattr("zleap.sag.core.ai.jev.aiohttp.ClientSession", forbidden_transport)
    response = await client.post("/api/v1/system/model-config/jev/test", headers=headers, json={"jev_endpoint": None})
    assert response.status_code == 200, response.text
    assert response.json()["ok"] is False


async def test_jev_draft_requires_authentication(jev_config_client):
    client, _headers, _resets = jev_config_client
    response = await client.post("/api/v1/system/model-config/jev/test", json={})
    assert response.status_code == 401


@pytest.mark.parametrize("path,method", [("/model-config/jev/test", "POST"), ("/model-config", "PUT")])
async def test_jev_validation_errors_do_not_echo_sensitive_input(jev_config_client, path, method):
    client, headers, _resets = jev_config_client
    secret = "synthetic-sensitive-key-" * 500
    response = await client.request(
        method, f"/api/v1/system{path}", headers=headers,
        json={"jev_api_key": secret, "jev_endpoint": "https://user:synthetic-password@jev.example/score"},
    )
    assert response.status_code == 422
    assert secret not in response.text
    assert "synthetic-password" not in response.text


async def test_invalid_saved_jev_candidate_never_mutates_runtime(jev_config_client):
    from sag_api.core.db import SessionLocal
    from sag_api.core.errors import ConfigurationError
    from sag_api.db.models import Setting
    from sag_api.services.settings_service import apply_startup_overrides

    before = settings.model_dump()
    async with SessionLocal() as session:
        session.add(Setting(scope="global", key="model_config", value={
            "jev_endpoint": "file:///secret", "jev_model": "invalid-saved-model",
        }))
        await session.commit()
    with pytest.raises(ConfigurationError):
        await apply_startup_overrides(SessionLocal)
    assert settings.model_dump() == before
