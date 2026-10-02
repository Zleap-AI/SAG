# Separate chatbot and query connections

This optional extension adds two independent connections in the existing Model
settings tab, both disabled by default. Chat generation, streaming, tools, history
compression, and query-side LLM processing can use their own LLM. Every
`SearchReader` retrieval path (including standalone search, MCP, OpenAI/Dify
retrieval integrations and event recall) can use another embedding endpoint.
Extraction, indexing, imports and vector rebuilds keep the original connections.
Stock sources remain untouched; the settings row requires no migration.

To avoid endpoint contention during graph extraction, enable both optional
connections and point the chatbot LLM and query embedding at serving capacity
separate from the extraction LLM. Different URLs on the same busy machine can
still compete for resources. Query embedding must match the indexed model and
dimensions, with matching server-side implementation as described below.
Saved changes apply to new requests; an in-flight ask keeps its captured endpoints.

## Setup

Apply the optional overlay to stock Compose; all feature sources mount read-only.
The dependency initializer installs cryptography and the audited LiteLLM version
into its own named volume before the API starts. The API launcher installs guarded
query hooks before importing stock routes, and the web build composes the two
settings sections in a disposable tree. Neither stock source nor stock Dockerfile
changes. No S3, PostgreSQL/RDS, EKS, extraction repair or extraction Responses
extension is required.

```sh
cp cloud/compose/.env.chatbot.example cloud/compose/.env.chatbot.local
# Supply connection values and the encryption key in this ignored file as needed.
docker compose -f compose.yaml -f cloud/compose/chatbot.yaml up --build -d
```

Open the existing Settings → Model tab. Both new connections default to disabled.
Keep one API process/replica: live settings are process-local. Environment changes
and encryption-key replacement require an API restart; UI saves take effect after
their database commit without restarting. If an encryption key changes, recreate
only the API after following the recovery procedure below.

## Environment and inheritance

| Variable | Default / behavior |
| --- | --- |
| `SAG_CHATBOT_LLM_ENABLED` | `false`; independent query/generation LLM |
| `SAG_CHATBOT_EMBEDDING_ENABLED` | `false`; independent query embedding endpoint |
| `SAG_LOCK_CHATBOT_CONFIG` | `false`; when true, environment controls both connections, persisted UI values are ignored, and UI controls are read-only |
| `SAG_CHATBOT_LLM_PROVIDER` | `openai`; also `anthropic`, `gemini`, `responses` |
| `SAG_CHATBOT_LLM_BASE_URL` | Empty selects the chosen protocol's official endpoint |
| `SAG_CHATBOT_LLM_MODEL` | Required when enabled; independent of extraction |
| `SAG_CHATBOT_LLM_API_KEY` | Optional; supply only when the endpoint requires authentication; never inherited from stock |
| `SAG_CHATBOT_LLM_RESPONSES_PROVIDER` | `openai`; also `azure`, `bedrock_runtime`, `bedrock_mantle` |
| `SAG_CHATBOT_LLM_RESPONSES_ENDPOINT` | Full endpoint ending in `/responses`, required for Responses |
| `SAG_CHATBOT_LLM_RESPONSES_API_VERSION` | Azure only; versioned endpoint requires a version; `/openai/v1/responses` defaults to `v1` |
| `SAG_CHATBOT_LLM_RESPONSES_SEND_TEMPERATURE` | `false`; existing Responses capability rules |
| `SAG_CHATBOT_LLM_RESPONSES_THINKING_CONFIG` | Optional complete model thinking JSON file, using the existing Responses schema |
| `SAG_CHATBOT_EMBEDDING_BASE_URL` | Required when enabled; OpenAI-compatible embedding base URL |
| `SAG_CHATBOT_EMBEDDING_API_KEY` | Optional; supply only when the endpoint requires authentication; never inherited from stock |
| `SAG_CHATBOT_CONFIG_ENCRYPTION_KEY` | Deployment-secret Fernet key, required to save UI-entered API keys |

Base URLs accept HTTP for local gateways and HTTPS for hosted endpoints, rejecting
embedded credentials, queries and fragments. Responses validates HTTPS, loopback,
Azure and Bedrock endpoints. Use TLS in production. Bedrock Responses uses bearer
API-key authentication; this connection does not implement SigV4.

Both optional connections can be enabled, saved and tested without an API key.
Keyless settings need no credential-encryption key. The LLM still requires a model,
and query embedding still requires an endpoint. Hosted services that require
authentication report a connection-test failure until their credentials are supplied.
To satisfy SDK factories without inheriting unrelated SDK/environment keys,
keyless requests use a fixed, nonsecret internal token. OpenAI-compatible chat,
embedding and Responses omit authentication headers; native Anthropic/Gemini
transports may send that placeholder token. Original extraction/indexing
configuration and credential requirements are unchanged.

Tuning inherits the **effective** original LLM settings, including stock UI
overrides. Optional environment overrides are `SAG_CHATBOT_LLM_TEMPERATURE`,
`_MAX_TOKENS`, `_CONTEXT_WINDOW`, `_TIMEOUT_MS`, `_MAX_RETRIES`,
`_STRUCTURED_OUTPUT_MODE`, and `_EXTRA_BODY`. Extra body must be a JSON object.
Vendor options are inherited only when the original and query protocols match;
another protocol starts with empty options unless overridden. Anthropic's
temperature rule still applies. Responses owns separate endpoint, credential,
native options, thinking rules and run reasoning state. The bundled
[model policy](sag_chatbot/responses/model_thinking.json) defines exact model IDs;
a configured thinking file replaces the complete policy.

Query embedding always uses the original model, effective schema dimensions and
effective request dimensions, including stock's provider/model exception that
omits request dimensions. `SAG_CHATBOT_EMBEDDING_MODEL`, `_DIMENSIONS`,
`_SCHEMA_DIMENSIONS` and `_REQUEST_DIMENSIONS` overrides are rejected. Count,
dimensions and finite numeric values are checked before query vectors reach
retrieval. Identical model names and dimensions **cannot prove identical
server-side weights, normalization or preprocessing**: administrators must deploy
matching implementations. The existing embedding model guard and vector rebuild
procedure remain enforced.

## UI, API and failures

The Model tab composes the stock form with two optional connection sections, each
with an enable switch and Test control aligned to the right, matching the original
connection footers. Buttons also match the original wording: **Test generation
model** for LLM and **Test embedding connection** for embedding, in both locales.
Testing disables only that optional connection's controls;
the other optional connection stays editable and can be tested concurrently.
Each test retains its own loading state and result. One **Save** button at the very
bottom saves edits across the page. Query embedding model/dimensions are informational.
The original generation Test button and result sit in the LLM section footer.
Additional tuning stays in the original controls or environment. UI values
override environment defaults unless locked, independently of the existing
embedding identity guard.

The single Save starts writes for changed, ready forms independently. Original
settings use the stock `PUT /api/v1/system/model-config` handler with only changed,
unlocked fields; unchanged originals are skipped. Both changed chatbot connections
still save together through the existing chatbot PUT. Optional-only edits never
call the original save or reset extraction engines. Unchanged chatbot connections
create no new UI overrides; deployment-locked settings are skipped. Blank keys
retain credentials.

If an original-model change waits for active extraction to finish before rebuilding
engines, chatbot changes save concurrently. Each form disables only its own controls
while saving. Once the chatbot write finishes, its controls and tests are available
again; new chatbot edits can use the same Save button while the original write is
still pending. A form's load failure or active test also does not prevent saving
changes in the other ready form. Save is disabled when neither form has eligible
changes. Each completed write reports its own success or failure and refreshes
capabilities and shared embedding information.

These writes are **not one database transaction**. A failure in one form does not
stop the other write. The page retains each failed draft and its keys, reports the
form that failed, and retries only the remaining edits on the next Save. The original
form clears keys only after its successful save. Both chatbot drafts clear their
keys only after the combined chatbot PUT succeeds. A capability-refresh
failure reports that settings were saved and asks for a page reload rather than
claiming a write failure. No automatic rollback or new persistence is introduced.
If a shared-identity refresh fails after loading, its Retry keeps connection drafts
and keys rather than replacing them with saved values.
Chat capabilities report the effective model/context and retain stock provider IDs;
Responses uses the OpenAI integration slot, with its exact provider exposed by this
extension's configuration API.

User-authenticated administration endpoints (connector tokens cannot administer):

```text
GET  /api/v1/system/chatbot-config
PUT  /api/v1/system/chatbot-config
POST /api/v1/system/chatbot-config/test
```

GET/PUT return `{ "config": ... }`, with credential-presence flags, per-field and
credential sources, effective tuning, shared embedding identity, encryption
availability and lock status. Extra-body contents are hidden because administrative
options can contain secrets. PUT accepts partial `llm`/`embedding` connection
objects; unknown fields and null values are rejected. No embedding identity or
tuning fields are accepted in UI drafts. Blank password fields retain the key.
Changing an endpoint or authentication provider without entering a new key drops
the previous UI key. A matching environment connection can supply its own key;
otherwise the new connection is keyless. An existing environment key cannot follow
a UI draft to another host/provider silently. Blank keys retain saved credentials
when the endpoint/provider identity stays the same.

POST accepts `{ "target": "llm", "llm": { ...connection fields... } }` or
`{ "target": "embedding", "embedding": { ...connection fields... } }`. Tests
use unsaved credentials without writing live state or persistence, and can test a
new key without encryption configured. The draft must enable the connection;
API keys are optional. Under a lock, no-draft API tests can test the environment
connection; UI editing/testing controls remain read-only.

Only UI-entered keys are Fernet encrypted into the existing global settings row
with `key=chatbot_config`. Environment keys stay outside the DB. Missing encryption
configuration prevents saving UI keys. Unreadable persisted keys fail startup
before job recovery with recovery instructions. A lock deliberately ignores the
persisted row, allowing environment-controlled recovery. Provider and validation
errors use fixed messages without credentials, ciphertext or provider bodies.
Failures never select another model/backend. Stock retrieval strategy fallback
(for example multi to vector) uses the configured query clients for both attempts.

HTTP operations and Agent runs capture their settings once. Nested retrieval
shares that snapshot via task-local scope. Only retrieval/LLM calls activate query
adapters: upload/enqueue requests and document workers retain original adapters.
Extension-owned engine clients are lazy, isolated per operation and closed when
its final scope finishes, including failure/cancellation. Responses HTTP clients
are owned by each transport call. LiteLLM SDK-managed pooling stays stock-owned.
Updates are validated before commit and published after commit; active operations
finish with captured settings. The encrypted row is the only new durable state.

## Encryption recovery and rotation

Generate a Fernet key through deployment secret tooling (32 random bytes encoded
as URL-safe base64). Keep it with database restore material, separately from the
DB; never track it or print it in diagnostics. Restore the original secret and
restart if the key is missing/wrong. If lost permanently, ciphertext cannot be
recovered: back up and remove **only** `scope=global, key=chatbot_config`, restart
with disabled or complete environment connections, then re-enter UI keys/settings.
Restoring the row without its key fails again. A complete locked environment can
serve temporarily while the row is repaired; remove the lock only after recovery.

For rotation, stop the API with no overlapping pods and back up its row. In an
isolated maintenance invocation of the existing image, supply the old key as
`SAG_CHATBOT_CONFIG_ENCRYPTION_KEY` and new key as
`SAG_CHATBOT_NEW_CONFIG_ENCRYPTION_KEY`. Re-encrypt in one transaction without
printing keys/plaintext:

```python
import asyncio, copy, os
from cryptography.fernet import Fernet
from sqlalchemy import select
from sag_api.core.db import SessionLocal, dispose_db
from sag_api.db.models import Setting

async def rotate():
    old = Fernet(os.environ["SAG_CHATBOT_CONFIG_ENCRYPTION_KEY"].encode())
    new = Fernet(os.environ["SAG_CHATBOT_NEW_CONFIG_ENCRYPTION_KEY"].encode())
    async with SessionLocal() as session:
        row = await session.scalar(select(Setting).where(
            Setting.scope == "global", Setting.key == "chatbot_config"))
        if row:
            value = copy.deepcopy(row.value)
            for entry in value.values():
                if entry.get("api_key_encrypted"):
                    entry["api_key_encrypted"] = new.encrypt(
                        old.decrypt(entry["api_key_encrypted"].encode())).decode()
            row.value = value
            await session.commit()
    await dispose_db()

asyncio.run(rotate())
```

Install the new serving secret before restart, remove the temporary new-key
variable, and verify GET and both Test controls. A failed transaction leaves the
row unchanged. If secret replacement fails after commit, keep the API stopped and
complete replacement, or restore both old row and old secret together. Keep
backups until verified.

## Disable, rollback and upgrades

Save each enable switch off. Environment switches alone cannot override an enabled
UI row: disable in UI, or lock to both environment switches false and restart.
Future operations use stock clients; no reindex is needed. To remove the feature,
omit its Compose override/Python mount and build the stock web image without the
overlay. Use the stock API entrypoint when removing the mount. Stock ignores
the isolated settings row; archive it and its secret separately. Files, vectors and
extraction configuration need no rollback migration.

| Guarded seam | Why / upgrade review |
| --- | --- |
| `settings_service.apply_startup_overrides` | Load encrypted settings after DB/stock overrides and before job recovery |
| All five `SearchReader` retrieval entry points | Capture scopes for single, bulk, fallback and direct event retrieval |
| `core.litellm_policy.apply_litellm_completion_policy` | Captured provider options and separate Responses thinking rules |
| `runtime._RuntimeFactory` client/policy factories | Generation facade and scoped policy settings |
| `agent_domain.prepare_ask/settings` | Non-HTTP preparation and effective history context budget |
| `system._capabilities` | Effective chatbot model/context |
| `AgentRuntime._drive` | One snapshot per tool loop, independent query reasoning replay |
| Engine adapter registry | Supported registration; original durable adapters remain owned by engines |
| Web Model form | Preserve stock fields/validation in a disposable build tree, expose changed-field saves/readiness to one independent page Save, move the original generation test to its section footer, and retain the quick-setup identity refresh |

Python/web manifests refuse changed upstream source fingerprints; Python also
requires audited zleap-sag/LiteLLM versions. Feature requirements pin LiteLLM to
1.103.0 so fresh overlay installations do not select an unaudited stock dependency release.
The filelock bound preserves the stock API requirement during target-directory installs.
Review method contracts, dependency
injection, import/startup order, data flow, teardown, security checks and error
behavior before updating fingerprints. Recheck dependency adapter/resource/AI
sources, credential reuse rules and the web form's save flows, including draft
capture, changed-field/null comparisons, deployment locks, readiness, independent
save completion, error propagation, key retention and shared identity refresh. The loader composes
subsequent import loaders instead of bypassing their transforms. Prefer supported
query-provider/UI extension seams if upstream adds them, removing obsolete patches.

Maintenance additions are seven backend modules (including the standalone
launcher), the query-only Responses package, two UI components, a guarded web build
overlay/Dockerfile, two compatibility manifests, feature dependencies and tests,
one Compose override with an environment example, and two verification/build
scripts. Every file is under `cloud/`; stock sources, dependencies and entrypoints
remain unchanged. The query Responses transport/codec serve only the optional
chatbot route and do not register or configure an extraction provider.

After upgrades, verify endpoint routing for generation, tools, history compression,
all retrieval entry points, extraction and indexing. Recheck immutable request
snapshots, cleanup after cancellation, encrypted settings recovery, deployment
locks and sanitized errors. The UI tests cover changed-field saves, independent
completion, failed-draft/key retention, identity refresh and concurrent connection
tests. Optional-only saves must never reset extraction engines.

Keyless endpoints use a fixed nonsecret SDK token without inheriting unrelated
credentials. The keyless embedding subclass wraps only its owned SDK
`embeddings.create` call to omit `Authorization` per request; SDK default headers
alone still trigger authentication validation. The guarded stock client must keep
calling that method without overriding `extra_headers`. Query Responses omits
headers in its owned transport. Stock retries, vector validation and client cleanup
remain in use; verify these seams with controlled transports before upgrading SDKs.

## Verification

Use Python 3.12 with API dependencies, audited LiteLLM 1.103.0 and feature test
dependencies. Tests use isolated DBs, generated encryption keys, endpoint spies and
controlled transports; no live provider credentials are required.

```sh
PYTHONPATH=cloud/chatbot:apps/api \
  LITELLM_LOCAL_MODEL_COST_MAP=True python -m pytest cloud/chatbot/tests
SAG_CHATBOT_API_IMAGE=sag-api:latest sh cloud/scripts/shared-test-chatbot.sh
sh cloud/scripts/shared-prepare-chatbot-web.sh /tmp/sag-chatbot-web-check
cd /tmp/sag-chatbot-web-check
npm ci
npm run typecheck
npm run test:unit -- components/features/model-config-form.test.tsx components/features/chatbot-config-sections.test.tsx lib/model-config-lock.test.ts
npm run build
```

After upstream merges run affected stock settings, Agent/chat, streaming,
retrieval, policy and document suites with the standalone launcher installed and
both connections disabled. Render the Compose configuration, syntax-check the
scripts, and run mounted tests against the intended stock API image. Check the
optional Compose stack with isolated volumes and fake endpoints before testing
real connections. The existing stock stack is unaffected when the overlay is
omitted.

Live validation is separate: test both optional connections, streaming and tool
turns, then inspect extraction/index/query traffic at their intended endpoints.
Mock success proves neither live AWS/Azure/Anthropic/Gemini
authentication nor matching server-side embedding implementations.
