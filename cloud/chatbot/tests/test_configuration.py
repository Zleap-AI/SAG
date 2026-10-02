import json

import pytest
from cryptography.fernet import Fernet
from sag_api.core.errors import ConfigurationError, ForbiddenError
from sag_chatbot import runtime as rt
from sag_chatbot.config import Environment


def enabled_env(**fields):
    return {
        "SAG_CHATBOT_LLM_ENABLED": "true", "SAG_CHATBOT_LLM_MODEL": "query-model",
        "SAG_CHATBOT_LLM_API_KEY": "query-key", "SAG_CHATBOT_LLM_BASE_URL": "https://query.invalid/v1",
        **fields,
    }


def test_defaults_and_independent_switches(isolate):
    assert not isolate.public()["llm"]["enabled"]
    assert not isolate.public()["embedding"]["enabled"]
    for fields, target in ((enabled_env(), "llm"), ({
        "SAG_CHATBOT_EMBEDDING_ENABLED": "true", "SAG_CHATBOT_EMBEDDING_BASE_URL": "https://embed.invalid/v1",
        "SAG_CHATBOT_EMBEDDING_API_KEY": "embedding-query-key",
    }, "embedding")):
        isolate.environment = Environment(fields)
        config = isolate.public()
        assert config[target]["enabled"]
        assert not config["embedding" if target == "llm" else "llm"]["enabled"]


def test_keyless_environment_connections_are_configured_without_stock_keys(isolate):
    isolate.stock.llm_api_key = None
    isolate.environment = Environment({
        "SAG_CHATBOT_LLM_ENABLED": "true", "SAG_CHATBOT_LLM_MODEL": "local-model",
        "SAG_CHATBOT_LLM_BASE_URL": "http://localhost:9000/v1",
        "SAG_CHATBOT_EMBEDDING_ENABLED": "true", "SAG_CHATBOT_EMBEDDING_BASE_URL": "http://localhost:9001/v1",
    })
    snapshot = isolate.snapshot()
    assert snapshot.settings.llm_configured and snapshot.settings.llm_api_key == ""
    assert not snapshot.stock.llm_configured
    assert all(not connection.api_key for connection in snapshot.connections.values())
    assert all(not isolate.public()[target]["api_key_set"] for target in ("llm", "embedding"))
    isolate.environment = Environment({})
    assert not isolate.snapshot().settings.llm_configured


@pytest.mark.parametrize("patch", [{"llm": {"enabled": True}}, {"embedding": {"enabled": True}}])
def test_keyless_connections_still_require_model_or_endpoint(isolate, patch):
    with pytest.raises(ConfigurationError, match="Invalid"):
        isolate.draft(patch, persist=False)


def test_disabled_extension_does_not_add_stock_tuning_constraints(isolate):
    # Stock permits these values; only an enabled new connection validates its tuning.
    isolate.stock.llm_temperature = 3
    isolate.stock.llm_max_tokens = 0
    isolate.stock.llm_context_window = 0
    active = isolate.snapshot().settings
    assert active.effective_llm_temperature == 3 and active.llm_max_tokens == 0 and active.llm_context_window == 0


@pytest.mark.parametrize("name", ["MODEL", "DIMENSIONS", "SCHEMA_DIMENSIONS", "REQUEST_DIMENSIONS"])
def test_embedding_independent_identity_rejected(name):
    with pytest.raises(ConfigurationError, match="original embedding"):
        Environment({"SAG_CHATBOT_EMBEDDING_" + name: "override"})


@pytest.mark.parametrize("provider,prefix,temp", [("openai", "openai/", .3), ("anthropic", "anthropic/", 1), ("gemini", "gemini/", .3)])
def test_provider_rules_and_protocol_extra_options(isolate, provider, prefix, temp):
    isolate.stock.llm_extra_body = {"vendor_specific": "secret-option"}
    isolate.environment = Environment(enabled_env(SAG_CHATBOT_LLM_PROVIDER=provider))
    snapshot = isolate.snapshot()
    assert snapshot.settings.routed_llm_model == prefix + "query-model"
    assert snapshot.settings.effective_llm_temperature == temp
    assert snapshot.settings.llm_extra_body == (isolate.stock.llm_extra_body if provider == "openai" else None)
    assert "secret-option" not in json.dumps(isolate.public())


def test_tuning_inherits_and_environment_overrides(isolate):
    isolate.stock.llm_context_window = 90000
    isolate.stock.llm_max_tokens = 500
    isolate.environment = Environment(enabled_env(
        SAG_CHATBOT_LLM_TEMPERATURE="0.7", SAG_CHATBOT_LLM_TIMEOUT_MS="11000",
        SAG_CHATBOT_LLM_MAX_RETRIES="4", SAG_CHATBOT_LLM_STRUCTURED_OUTPUT_MODE="json_object",
        SAG_CHATBOT_LLM_EXTRA_BODY='{"reasoning_effort":"low"}',
    ))
    active = isolate.snapshot().settings
    assert active.llm_context_window == 90000 and active.llm_max_tokens == 500
    assert active.llm_temperature == .7 and active.llm_timeout_ms == 11000 and active.llm_max_retries == 4
    assert active.llm_structured_output_mode == "json_object" and active.llm_extra_body == {"reasoning_effort": "low"}


@pytest.mark.parametrize("name,value", [
    ("ENABLED", "yes"), ("TEMPERATURE", "NaN"), ("TIMEOUT_MS", "3"), ("MAX_RETRIES", "11"),
    ("MAX_TOKENS", "0"), ("EXTRA_BODY", "[]"), ("STRUCTURED_OUTPUT_MODE", "invalid"),
])
def test_invalid_env_fails_secret_safely(isolate, name, value):
    with pytest.raises(ConfigurationError) as caught:
        isolate.environment = Environment(enabled_env(**{"SAG_CHATBOT_LLM_" + name: value}))
        isolate.snapshot()
    assert "query-key" not in str(caught.value)


@pytest.mark.parametrize("provider,endpoint,version", [
    ("openai", "https://responses.invalid/v1/responses", ""),
    ("azure", "https://azure.invalid/openai/v1/responses", ""),
    ("azure", "https://azure.invalid/openai/responses", "2025-04-01-preview"),
    ("bedrock_runtime", "https://bedrock-runtime.us-east-1.amazonaws.com/openai/v1/responses", ""),
    ("bedrock_mantle", "https://bedrock-mantle.us-east-1.api.aws/v1/responses", ""),
])
def test_all_responses_providers_and_independent_policy(isolate, provider, endpoint, version):
    isolate.environment = Environment(enabled_env(
        SAG_CHATBOT_LLM_PROVIDER="responses", SAG_CHATBOT_LLM_RESPONSES_PROVIDER=provider,
        SAG_CHATBOT_LLM_RESPONSES_ENDPOINT=endpoint, SAG_CHATBOT_LLM_RESPONSES_API_VERSION=version,
    ))
    snapshot = isolate.snapshot()
    assert snapshot.settings.routed_llm_model == "sag_chatbot_responses/query-model"
    assert snapshot.responses_config.provider == provider
    assert snapshot.responses_config.endpoint.startswith(endpoint)


async def test_encryption_precedence_retention_and_restart(isolate, database):
    isolate.environment = Environment({**enabled_env(), "SAG_CHATBOT_CONFIG_ENCRYPTION_KEY": Fernet.generate_key().decode()})
    async with database() as session:
        public = await isolate.save(session, {"llm": {"model": "ui-model", "api_key": "ui-secret"}})
        row = await isolate.row(session)
        assert "ui-secret" not in json.dumps(row.value)
        assert "query-key" not in json.dumps(row.value)
        assert public["llm"]["credential_source"] == "ui"
        assert public["llm"]["sources"]["model"] == "ui"
        await isolate.save(session, {"llm": {"api_key": "", "model": "ui-model-2"}})
    restarted = rt.Manager(isolate.environment, isolate.stock)
    await restarted.load(database)
    assert restarted.connections()["llm"].api_key == "ui-secret"
    assert restarted.connections()["llm"].model == "ui-model-2"


async def test_missing_wrong_key_and_lock(isolate, database):
    async with database() as session:
        await isolate.save(session, {"llm": {"api_key": "ui-secret"}})
    wrong = rt.Manager(Environment({"SAG_CHATBOT_CONFIG_ENCRYPTION_KEY": Fernet.generate_key().decode()}), isolate.stock)
    with pytest.raises(ConfigurationError, match="restore the original"):
        await wrong.load(database)
    missing = rt.Manager(Environment({}), isolate.stock)
    with pytest.raises(ConfigurationError, match="SAG_CHATBOT_CONFIG_ENCRYPTION_KEY"):
        await missing.load(database)
    with pytest.raises(ConfigurationError):
        missing.draft({"llm": {"api_key": "ui-secret"}}, persist=True)
    locked = rt.Manager(Environment({"SAG_LOCK_CHATBOT_CONFIG": "true"}), isolate.stock)
    await locked.load(database)
    assert locked.public()["llm"]["credential_source"] == "unset"
    with pytest.raises(ForbiddenError):
        locked.draft({"embedding": {"enabled": True}}, persist=False)


@pytest.mark.parametrize("target", ["llm", "embedding"])
async def test_identity_changes_drop_previous_keys_and_allow_keyless_endpoints(isolate, database, target):
    fields = {"enabled": True, "base_url": "https://a.invalid/v1", "api_key": "a-key"}
    if target == "llm":
        fields["model"] = "m"
    async with database() as session:
        await isolate.save(session, {target: fields})
        before = json.dumps(isolate.persisted)
        _, draft = isolate.draft({target: {"base_url": "https://b.invalid/v1"}}, persist=False)
        assert draft.connections[target].api_key == "" and json.dumps(isolate.persisted) == before
        await isolate.save(session, {target: {"base_url": "https://b.invalid/v1", "api_key": ""}})
        assert isolate.connections()[target].api_key == ""
        assert "api_key_encrypted" not in isolate.persisted[target]
        await isolate.save(session, {target: {"api_key": "b-key"}})
        if target == "llm":
            await isolate.save(session, {target: {"provider": "anthropic"}})
            assert isolate.connections()[target].api_key == ""


async def test_identity_change_can_return_to_matching_environment_credentials(isolate, database):
    async with database() as session:
        await isolate.save(session, {"llm": {"enabled": True, "model": "m", "base_url": "https://a.invalid/v1", "api_key": "a-key"}})
    isolate.environment = Environment(enabled_env(SAG_CHATBOT_CONFIG_ENCRYPTION_KEY=isolate.environment.encryption_key))
    saved, snapshot = isolate.draft({"llm": {"base_url": "https://query.invalid/v1"}}, persist=True)
    assert snapshot.connections["llm"].api_key == "query-key"
    assert "api_key_encrypted" not in saved["llm"]


async def test_publication_after_commit_failure(isolate):
    class FailingSession:
        async def scalar(self, _): return None
        def add(self, _): pass
        async def commit(self): raise RuntimeError("database unavailable")
    before = isolate.persisted
    with pytest.raises(RuntimeError):
        await isolate.save(FailingSession(), {"llm": {"api_key": "new-key"}})
    assert isolate.persisted == before


def test_unsaved_draft_needs_no_encryption_and_never_mutates(isolate):
    isolate.environment = Environment({})
    _, snapshot = isolate.draft({"llm": {"enabled": True, "model": "draft", "api_key": "ephemeral"}}, persist=False)
    assert snapshot.connections["llm"].api_key == "ephemeral"
    assert isolate.persisted == {} and not isolate.connections()["llm"].enabled


def test_malformed_persistence_and_invalid_encryption_fail_actionably(isolate):
    for row in ("invalid", {"llm": {"api_key": "plaintext"}}, {"embedding": []}):
        with pytest.raises(ConfigurationError, match="Invalid saved chatbot_config") as caught:
            isolate.snapshot(row)
        assert "plaintext" not in str(caught.value)
    with pytest.raises(ConfigurationError, match="valid Fernet"):
        Environment({"SAG_CHATBOT_CONFIG_ENCRYPTION_KEY": "invalid-key"})


async def test_protocol_default_sources_and_forced_provider_temperature(isolate):
    from sag_api.core.litellm_policy import apply_litellm_completion_policy
    isolate.environment = Environment(enabled_env(SAG_CHATBOT_LLM_PROVIDER="anthropic"))
    config = isolate.public()
    assert config["llm"]["tuning_sources"]["extra_body"] == "protocol_default"
    assert config["llm"]["tuning_sources"]["temperature"] == "provider"
    async with isolate.scope(query=True) as snapshot:
        request = apply_litellm_completion_policy(snapshot.settings, {"model": "anthropic/query-model", "temperature": 0})
        assert request["temperature"] == 1
