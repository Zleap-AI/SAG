# Responses API generation

In Settings → Model, select **OpenAI-compatible** as the provider protocol, then
choose **Responses API** as the API format, for the original generation connection
or the optional independent chatbot LLM. One OpenAI-compatible connection supports
Azure OpenAI v1, Bedrock Runtime, Bedrock Mantle, and compatible Responses endpoints. Extraction,
evaluation, retrieval generation, streaming, and Agent function tools use the existing
SAG generation and engine interfaces. Embeddings keep their own connection.

## Configuration

Enter the exact model/deployment ID. In Responses format, **Base URL** requires the
**full endpoint ending in `/responses`**. In Chat Completions format, Base URL is
the API root. Enter the model/deployment ID and the service's API key; no Responses
service selector is needed. Send temperature appears under Responses options.
The default Bearer authentication is shared by OpenAI-compatible services, Azure
v1, and Bedrock. The endpoint selects the service; SAG does not infer a vendor from
its hostname.

The UI groups Chat Completions and Responses under OpenAI-compatible. API and
environment configuration still use `provider=responses` to select Responses;
existing saved connections and deployment configuration remain compatible.
For environment setup:

```dotenv
SAG_LLM_PROVIDER=responses
SAG_LLM_MODEL=YOUR_MODEL_ID
SAG_LLM_API_KEY=YOUR_API_KEY
SAG_LLM_RESPONSES_PROVIDER=openai
SAG_LLM_RESPONSES_ENDPOINT=https://api.openai.com/v1/responses
```

The existing database settings precedence and `SAG_LOCK_LLM_CONFIG` still apply.
The lock includes the Responses authentication override, endpoint, API version, and temperature
switch. Invalid environment or saved configuration fails before workers start.
UI/API changes are validated before they are committed. Changing the Responses
authentication, endpoint, or protocol clears an existing key unless a new key is supplied.

For a separate chatbot connection, use Settings → Model or configure:

```dotenv
SAG_CHATBOT_LLM_ENABLED=true
SAG_CHATBOT_LLM_PROVIDER=responses
SAG_CHATBOT_LLM_MODEL=YOUR_CHAT_MODEL_ID
SAG_CHATBOT_LLM_API_KEY=YOUR_CHAT_API_KEY
SAG_CHATBOT_LLM_RESPONSES_PROVIDER=openai
SAG_CHATBOT_LLM_RESPONSES_ENDPOINT=https://api.openai.com/v1/responses
```

Independent chatbot credentials and endpoint options belong to the existing
operation snapshot. UI keys use the existing encrypted settings storage; see
[chatbot connections](chatbot-connections.md) for encryption, locks, recovery,
and query embedding constraints. A blank key on an independent connection sends
no authentication header, for a gateway that handles authentication itself.
Original generation still requires a key. Responses keys never become embedding keys.

| Service | Full endpoint | Authentication |
| --- | --- | --- |
| OpenAI-compatible | `https://HOST/v1/responses` | Bearer API key |
| Azure v1 | `https://RESOURCE.openai.azure.com/openai/v1/responses` | Bearer Azure API key; use the deployment name as the model |
| Bedrock Runtime | `https://bedrock-runtime.REGION.amazonaws.com/openai/v1/responses` | Bearer Bedrock API key |
| Bedrock Mantle | `https://bedrock-mantle.REGION.api.aws/v1/responses` | Bearer Bedrock API key |
| Legacy Azure (advanced override) | `https://RESOURCE.openai.azure.com/openai/responses` | `api-key`; set the API version |

For older Azure connections or Azure gateways requiring the `api-key` header, open
**Advanced connection settings**, select **API key header (Azure)**, and configure
the Azure API version. In deployment configuration, set
`SAG_LLM_RESPONSES_PROVIDER=azure` or `SAG_CHATBOT_LLM_RESPONSES_PROVIDER=azure` and
the corresponding `SAG_*_RESPONSES_API_VERSION`. This override also accepts the
Azure `/openai/v1/responses` path and defaults its version to `v1`. A conflicting
endpoint query version is rejected. In default Bearer mode, Azure v1 requires no
separate API-version setting; an explicit version may be included in the full URL.

Existing saved Azure authentication/version overrides are preserved, including when
only model or temperature settings change. The existing API/environment field
names remain compatible. `openai` is the default Bearer mode; saved
`bedrock_runtime` and `bedrock_mantle` values remain Bearer compatibility aliases
without vendor-specific host/path restrictions. No configuration migration is required.

See the official [Azure Responses reference](https://learn.microsoft.com/en-us/rest/api/microsoft-foundry/azureopenai/responses)
and [Bedrock Responses guide](https://docs.aws.amazon.com/bedrock/latest/userguide/bedrock-mantle.html)
for endpoint, model, and authentication requirements.

HTTPS is required; HTTP loopback is allowed for local testing.
This implementation uses API keys. It does not implement Azure managed identity,
AWS SigV4/IAM discovery, native Bedrock Converse, or provider-hosted tools.
Endpoint/model availability must be verified against the intended deployment.

## Request options and recovery

- Text, user images, function tools/results, forced tool choices, JSON schema, and
  JSON object output are translated to the Responses protocol. Function schemas
  retain `strict=false` unless explicitly requested.
- `SAG_LLM_MAX_TOKENS` maps to `max_output_tokens`. Temperature is omitted unless
  `SAG_LLM_RESPONSES_SEND_TEMPERATURE=true`; the independent chatbot switch is
  `SAG_CHATBOT_LLM_RESPONSES_SEND_TEMPERATURE`.
- Reasoning options remain administrative. Use `SAG_LLM_EXTRA_BODY` or
  `SAG_CHATBOT_LLM_EXTRA_BODY`, for example `{"reasoning":{"effort":"low"}}`.
  Allowed fields are `reasoning`, `text`, `temperature`, `top_p`,
  `max_output_tokens`, `truncation`, and `service_tier`. Explicit native reasoning
  takes precedence over engine defaults. Unsupported flags fail explicitly.
- The bundled `sag_api/core/responses_thinking.json` contains the existing Qwen and
  DeepSeek mappings. An unknown Qwen model requires explicit native effort or a
  replacement rules file selected by `SAG_LLM_RESPONSES_THINKING_CONFIG` or
  `SAG_CHATBOT_LLM_RESPONSES_THINKING_CONFIG`. Rules use exact lowercase model IDs,
  `default_effort`, and optional `legacy_disabled_field`. Files are loaded as
  immutable policies; restart after changing them. Verify supported effort levels
  on the actual endpoint; SAG does not silently substitute another level.
- Requests always use `store=false`. SAG owns conversation history. Returned
  encrypted reasoning and output items are replayed across retained Agent tool
  turns, isolated from simultaneous runs and generation inside tools. Replay state
  is cleared after success, failure, or cancellation. A restart uses SAG's durable
  history; interrupted provider streams and provider response IDs are not resumed.
- Only successfully completed function calls reach execution. Incomplete,
  refused, malformed, or prematurely terminated streams fail. Explicit retries
  cover temporary failures before streamed output begins; partially delivered
  streams are never silently restarted. Provider bodies and credentials are
  excluded from errors. The existing explicit unsupported-schema fallback remains
  available in `auto` structured-output mode.

See the [official Responses migration guide](https://developers.openai.com/api/docs/guides/migrate-to-responses)
for request shapes and stateless reasoning continuation.

## Implementation and verification

The native implementation uses `core/model_providers.py`, validated Settings and
chatbot drafts, the supported LiteLLM custom-provider registry, and SAG's existing
`QueryAgentRuntime` subclass. It does not install import loaders, replace functions
or classes, rewrite source ASTs, or require cloud bootstrap code, checksum guards,
the S3 extension, or mounted extensions. The existing stock LiteLLM policy lifecycle
is retained; Responses adds a native policy branch and provider registration.

The maintenance surface is the Responses codec, HTTP/SSE adapter, request routing,
endpoint validation, and model reasoning rules. LiteLLM 1.103.0 is the integration
baseline. After dependency upgrades, run the actual generation, extraction,
schema fallback, retry, usage, concurrent Agent replay, and separate-connection
probes, along with the affected stock regression suites:

```sh
cd apps/api
uv sync --extra dev
uv run pytest tests/responses tests/chatbot tests/test_settings.py \
  tests/test_litellm_policy.py tests/test_agent_runtime.py tests/test_storage_bootstrap_runtime.py
cd ../web
npm ci
npm run test:unit -- components/features/model-config-form.test.tsx \
  components/features/chatbot-config-sections.test.tsx
npm run typecheck
npm run i18n:check
```

The tests use controlled HTTP transports and disposable databases. They do not
establish live OpenAI, Azure, Bedrock, Qwen, or DeepSeek acceptance. Before production,
verify authentication, extraction/schema behavior, text streaming, multi-turn tools,
usage accounting, reasoning replay, cancellation, and supported model options on
your intended service. Disable Responses by selecting an existing provider and
supplying that connection's endpoint/model/key; there is no schema migration.

For administrators migrating from the cloud extension, select
`SAG_LLM_PROVIDER=responses`, rename `SAG_RESPONSES_*` to `SAG_LLM_RESPONSES_*`, and
remove the Responses bootstrap/mount. Existing independent chatbot Responses field
names stay compatible. Cloud S3 and deployment adaptations are separate features.
