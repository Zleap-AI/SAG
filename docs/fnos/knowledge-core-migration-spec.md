# fnOS Native knowledge reset

Supersedes the previous opt-in batch reingestion contract, as confirmed by the user on 2026-09-29.

- Native x86 only; preserve gateway, identity, tenant isolation and Public MCP.
- Before upgrading or reinstalling over retained user data, warn that the algorithm/index update resets existing knowledge and requires document re-upload. Default to decline; no reset without administrator consent and a verified cold backup.
- Once per existing tenant, clear knowledge sources, documents, source bindings, jobs, exploration records and universe snapshots; mark old internal chat citations stale. Preserve model settings, users, agents, conversations, document original files and old engine files in recovery storage.
- Also reset the earlier 0.13 reingestion candidate state. Use a separate engine-v0.13-clean directory, never the previous engine or engine-v0.13 store.
- Commit the reset marker with metadata changes. Restart and later routine updates must preserve newly uploaded knowledge. Fresh installs do not reset new content.
- Remove reingestion API, missing-original replacement, polling provider and persistent UI reminders. Users import knowledge through the normal upload flow.
- Keep cold backups and old stores until manual administrator cleanup; do not automatically delete original files or chat attachments.
- Tests: real 0.7.1 two-tenant reset, existing 0.13 marker, retained-file integrity, settings/chat preservation, restart idempotency, no automatic model calls, removed endpoint, install/upgrade consent and verified backup.
- This changes the reset policy only; index_rolled_back in the normal indexing path still requires independent diagnosis. Do not claim the reset cures this error or claim physical-device acceptance from automated tests.
