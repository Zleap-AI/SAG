import pytest

from sag_api.core.responses import endpoint_url, load_thinking_rules


def test_invalid_settings_do_not_echo_keys_and_blank_thinking_path_uses_defaults():
    from sag_api.core.config import Settings

    with pytest.raises(ValueError) as caught:
        Settings(
            _env_file=None,
            llm_provider="responses",
            llm_model="model",
            llm_api_key="private-key",
            llm_responses_endpoint="https://private:secret@host.invalid/v1/responses",
        )
    assert "private-key" not in str(caught.value) and "secret@" not in str(caught.value)
    settings = Settings(_env_file=None, llm_provider="responses", llm_model="model", llm_responses_thinking_config="")
    assert settings.routed_llm_model == "sag_responses/model"


@pytest.mark.parametrize(
    "content",
    [
        "{secret",
        "[]",
        '{"m": {"default_effort": "low"}, "m": {"default_effort": "high"}}',
        '{"m": {"default_effort": "low", "default_effort": "high"}}',
        '{"M": {"default_effort": "none"}}',
        '{"vendor/m": {"default_effort": "none"}}',
        '{"m": {"default_effort": ""}}',
        '{"m": {"default_effort": null}}',
        '{"m": {"default_effort": "low", "legacy_disabled_field": "secret"}}',
        '{"m": {"default_effort": "low", "api_key": "secret"}}',
    ],
)
def test_invalid_model_thinking_file_fails_without_content_in_error(tmp_path, content):
    path = tmp_path / "thinking.json"
    path.write_text(content)
    with pytest.raises(ValueError, match="cannot load model thinking rules") as error:
        load_thinking_rules(path)
    assert "secret" not in str(error.value)


@pytest.mark.parametrize(
    "provider,url,version,expected",
    [
        ("openai", "https://example.test/v1/responses", "", "https://example.test/v1/responses"),
        (
            "azure",
            "https://example.test/openai/responses",
            "2025-04-01-preview",
            "https://example.test/openai/responses?api-version=2025-04-01-preview",
        ),
        (
            "azure",
            "https://example.test/openai/responses?route=a%2Fb&api-version=2025-04-01-preview",
            "",
            "https://example.test/openai/responses?route=a%2Fb&api-version=2025-04-01-preview",
        ),
        (
            "azure",
            "https://example.test/openai/v1/responses/",
            "",
            "https://example.test/openai/v1/responses?api-version=v1",
        ),
        (
            "azure",
            "https://example.test/openai/v1/responses",
            "preview",
            "https://example.test/openai/v1/responses?api-version=preview",
        ),
        (
            "bedrock_runtime",
            "https://bedrock-runtime.us-east-1.amazonaws.com/openai/v1/responses",
            "",
            "https://bedrock-runtime.us-east-1.amazonaws.com/openai/v1/responses",
        ),
        (
            "bedrock_mantle",
            "https://bedrock-mantle.us-east-1.api.aws/v1/responses",
            "",
            "https://bedrock-mantle.us-east-1.api.aws/v1/responses",
        ),
    ],
)
def test_urls(provider, url, version, expected):
    assert endpoint_url(url, provider, version) == expected


@pytest.mark.parametrize(
    "provider,url,version",
    [
        ("azure", "https://example.test/openai/responses", ""),
        ("azure", "https://example.test/openai/responses?api-version=a", "b"),
        ("azure", "https://example.test/openai/responses?api-version=a&api-version=a", ""),
        ("azure", "https://example.test/openai/v1/responses?api-version=", ""),
        ("azure", "https://example.test/openai/deployments/model/responses", "v1"),
        ("openai", "https://secret:password@example.test/v1/responses", ""),
        ("openai", "https://example.test/v1/responses#secret", ""),
        ("openai", "https://example.test/v1", ""),
        ("openai", "https://example.test/v1/responses", "v1"),
        ("openai", "http://example.test/v1/responses", ""),
        ("openai", "https://example.test:broken/v1/responses", ""),
        ("bedrock_runtime", "https://bedrock-runtime.us-east-1.amazonaws.com/openai/v1/chat/completions", ""),
        ("bedrock_mantle", "http://bedrock-mantle.us-east-1.api.aws/v1/responses", ""),
    ],
)
def test_invalid_urls(provider, url, version):
    with pytest.raises(ValueError) as error:
        endpoint_url(url, provider, version)
    assert "password" not in str(error.value)


@pytest.mark.parametrize("provider", ["bedrock_runtime", "bedrock_mantle"])
def test_saved_bedrock_modes_accept_a_new_compatible_gateway_without_host_inference(provider):
    endpoint = "https://gateway.example/tenant/responses"
    assert endpoint_url(endpoint, provider) == endpoint
