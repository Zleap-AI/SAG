# Optional chatbot connections

SAG normally shares its LLM and embedding connections between document processing
and interactive queries. Settings → Models offers two independent switches:

- **Chatbot LLM** selects a separate provider, model, endpoint, and optional API key
  for answer generation, tool turns, history compression, and query-side LLM work.
  It uses OpenAI-compatible Chat Completions or Responses API, Anthropic, and Gemini.
- **Query embedding** selects a separate OpenAI-compatible endpoint and optional
  API key for retrieval. Document embeddings continue using the original endpoint.

Both switches default to disabled. Original model settings remain the source for
extraction/indexing, and disabling either switch restores its original connection.
This feature does not add a new provider protocol or change stored vectors.

## Configuration

Use the existing Models page or set these variables in `.env` and restart the API.
The normal `compose.yaml` passes them through; no alternate launcher is needed.

| Variable | Default / meaning |
| --- | --- |
| `SAG_CHATBOT_LLM_ENABLED` | `false` |
| `SAG_CHATBOT_LLM_PROVIDER` | `openai`; also `anthropic`, `gemini`, or `responses` |
| `SAG_CHATBOT_LLM_MODEL` | Required when the separate LLM is enabled |
| `SAG_CHATBOT_LLM_BASE_URL` | Blank selects the provider's official endpoint |
| `SAG_CHATBOT_LLM_API_KEY` | Optional for keyless OpenAI-compatible endpoints |
| `SAG_CHATBOT_EMBEDDING_ENABLED` | `false` |
| `SAG_CHATBOT_EMBEDDING_BASE_URL` | Required when query embedding is enabled |
| `SAG_CHATBOT_EMBEDDING_API_KEY` | Optional; blank sends no Authorization header |
| `SAG_LOCK_CHATBOT_CONFIG` | `false`; `true` uses only deployment settings |
| `SAG_CHATBOT_CONFIG_ENCRYPTION_KEY` | Stable Fernet key for UI-saved API keys |

Use HTTP(S) base URLs without embedded credentials, query strings, or fragments.
Responses uses a full endpoint ending in `/responses` and permits query parameters;
see [Responses configuration](responses-api.md) for the default Bearer connection
and advanced Azure authentication/version overrides. There is no Responses vendor selector.
Keyless local endpoints are supported. An optional connection never borrows an
original or SDK-environment API key. Changing the endpoint or provider clears the
previous UI key unless a new key is supplied; a matching deployment connection can
supply its own deployment key. A blank key keeps the current key when the connection
identity is unchanged. Changing only the model retains the key.

Query embedding always uses the original `SAG_EMBEDDING_MODEL`, effective schema
dimensions, and request dimensions, including the original rule for omitting an
unsupported `dimensions` parameter. Independent query model/dimension settings are
unsupported. Returned vector count, dimensions, and finite numeric values are
validated. The administrator must select an endpoint with the same model and vector
space; equal dimensions alone do not establish compatibility.

The separate LLM inherits original tuning unless deployment variables override it:
`SAG_CHATBOT_LLM_TEMPERATURE`, `MAX_TOKENS`, `CONTEXT_WINDOW`, `TIMEOUT_MS`,
`MAX_RETRIES`, `STRUCTURED_OUTPUT_MODE`, and `EXTRA_BODY` (all share the
`SAG_CHATBOT_LLM_` prefix). Blank values mean inheritance. `EXTRA_BODY` is a JSON
object. Provider temperature restrictions still apply; vendor-specific extra body
options are inherited only when the original and chatbot protocols match.

## Saving and credentials

The existing page-wide Save writes original model settings and optional connections
independently. Unchanged original settings are skipped. An original write waiting
for active extraction does not block optional saves/tests. Partial success is shown
per group, and failed drafts remain available for retry. Connection Tests use the
unsaved draft without activating or persisting it.

The original **Embedding model** section also has a Test button. Its authenticated
`POST /api/v1/system/model-config/embedding/test` endpoint accepts an unsaved
original embedding draft, returns the resulting dimensions, and never writes
settings or rebuilds vectors. Blank keys retain saved keys; blank URLs follow
the original generation provider's credential-reuse rules. Responses credentials
are never reused for embeddings. This test bypasses independent query connections
and closes its client after success, failure, or cancellation.

Authenticated users have the same settings access as existing model configuration.
The API is `GET /api/v1/system/chatbot-config`, `PUT` at that path, and
`POST /api/v1/system/chatbot-config/test` with `target` set to `llm` or `embedding`.
Keys are never returned. Provider failures and validation responses hide keys and
provider response bodies.

UI values use the existing Settings table, global scope, key `chatbot_config`; no
schema migration is needed. UI API keys are Fernet-encrypted before database commit.
Environment keys stay in deployment configuration and are not copied into this row.
Non-secret and keyless settings can be saved without an encryption key. To save keys,
generate a key with the API environment and supply it through deployment secrets:

```sh
python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
```

Keep this key stable across restarts and back it up separately from the database.
Do not commit it. `SAG_LOCK_LLM_CONFIG` controls only original LLM settings;
`SAG_LOCK_CHATBOT_CONFIG` independently locks both optional connections and ignores
the saved optional row.

## Runtime and recovery

Each request/agent operation captures immutable settings. Search reader entry
points also capture settings for standalone calls. Nested and concurrent tool
retrieval shares the active snapshot. A successful save affects subsequent
operations without changing an in-flight tool loop or rebuilding document engines.
Durable extraction/indexing adapters retain their original clients. Query clients
close when the operation ends, including failures and cancellation. Keyless query
embeddings use a native SDK adapter with ordinary request options, upstream text
truncation, and SDK timeout/retry configuration; SDK methods are never replaced.

Invalid enabled settings, malformed persisted configuration, or undecryptable UI
keys fail startup before background queues start. Restore the original encryption
key to recover. If that is impossible, back up the database, stop the API, remove
only the global `chatbot_config` Settings row, then restart and re-enter the
connections/keys. A deployment lock with complete environment configuration can
bypass an unreadable saved row while arranging recovery. For key rotation, first
remove that row during maintenance, install the replacement encryption key, restart,
and re-enter keys; existing ciphertext is not automatically re-encrypted.

Disable the switches in the UI to return to original connections. With locked
configuration, set the enabled variables to `false` and restart. Keep the optional
row/key backup if rolling back to an older SAG version. This release uses the
existing process-local settings model: run one API process for immediate UI updates;
multiple workers require coordinated restarts after saves. Standalone MCP stdio
loads persisted model/optional settings at startup and installs the same query
policy; restart that process to pick up later UI changes.

## Verification and upgrades

From `apps/api`, with the normal development/test dependencies installed:

```sh
pytest tests/chatbot
python tests/chatbot/smoke.py disabled
python tests/chatbot/smoke.py query
ruff check sag_api tests/chatbot
```

From `apps/web`:

```sh
npm run typecheck
npm run test:unit -- components/features/model-config-form.test.tsx components/features/chatbot-config-sections.test.tsx lib/model-config-lock.test.ts
npm run i18n:check
npm run lint
npm run build
```

The smoke checks use isolated temporary storage and mocked HTTP providers through
real SDK transports. They do not validate a live external service. Test deployment
credentials/endpoints with the UI Tests before enabling them.

After updating zleap-sag, run the feature tests and affected generation, agent,
retrieval, settings, and document-processing regressions. Review adapter registry
factories, embedding client/request behavior, SearchReader entry points, AgentRuntime
operation boundaries, and LiteLLM completion policy. These are ordinary native
integration seams; there are no source rewrites or checksum guards.
