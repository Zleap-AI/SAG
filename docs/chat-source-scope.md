# Source scope in chat history

Select one or more sources with `@` in the chat composer to limit that question's
knowledge search. The sent question shows `@SourceName` badges during generation
and after reopening or reloading the conversation. Questions sent without an
explicit selection show **Default source scope**, which uses the Agent's existing
default or bound sources. The composer keeps its existing selection behavior.
Document uploads that select a source in the composer use the same badges.

Badges describe the requested search scope. They do not assert that the Agent
searched every selected source or used its contents in the answer. Inspect the
response's references and retrieval steps for actual evidence.

Each user message saves source ID/name snapshots. Renaming or deleting a source
does not change previous badges. Retry uses the original question's saved IDs,
including an empty selection for default scope, instead of the current composer
selection. Deleted IDs retain the existing unavailable-source behavior; they do
not cause fallback to default sources. A new request for an unavailable source
displays its ID when its name can no longer be resolved.

Older messages have unknown scope and display no label. The UI never reconstructs
their selection from references, retrieval steps, or prompt previews. Retrying an
older message retains the prior behavior of using the current composer selection.

## Storage and upgrade

The message API adds `source_scope`: a list of `{id, name}` snapshots for user
messages, `[]` for recorded default scope, and `null` for unknown legacy scope or
assistant messages. `run.started` includes the same saved selection for the user
message, independently of the run's resolved `sources`. Names come from the server;
clients still submit only `source_ids` to the ask API.

Startup's existing idempotent schema updater adds nullable JSON column
`messages.source_scope_json`. Deployments with a separate production migration
runner must apply the equivalent `ALTER TABLE messages ADD COLUMN source_scope_json
JSON` before starting the updated API. No backfill is performed. Rebuild API and
web together. Older code ignores the additional nullable column; preserve it when
rolling back so historical selections survive a subsequent upgrade.

## Verification

From `apps/api`, run:

```sh
python -m pytest -q tests/test_message_source_scope.py tests/test_agents.py tests/test_message_pagination.py tests/test_experience.py tests/test_agent_tools.py
```

From `apps/web`, run:

```sh
npm run test:unit -- lib/conversation-runtime.test.ts components/features/chat/conversation-transcript.test.tsx components/features/chat/conversation-panel.test.tsx
npm run typecheck
npm run lint
npm run i18n:check
```

After an upstream upgrade, verify selection snapshot persistence, source rename
and deletion, legacy/default distinctions, live/historical badges, and retry scope.
The implementation adds one nullable message column and uses the existing
composer, transcript, transport, and schema updater; it adds no runtime dependency.
