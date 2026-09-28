# fnOS Native knowledge core migration

Baseline: `origin/fnos/develop` at `8ad465446d89067337b7406bff09fe44438f2059`.
Reference: `origin/main` at `3c5e937be1b4fad62bf9f4ae2b5bffa57d87816d`.
Refresh both refs before comparing or delivering.

## Product contract

- Deliver one installable Native x86 FPK and SHA-256 as a **candidate**. No fnOS device is available for acceptance; never label installation, upgrade, or rollback as device verified. Do not publish a Release unless explicitly requested.
- Include independent cursor, retry-pause, safe math rendering, model-config warning, extraction-content guard, and application-layer query analysis fixes. Adapt to fnOS rather than merging main.
- Upgrade `zleap-sag` from the locked 0.7.1 to 0.13.0. Add spreadsheet-original/record-boundary ingestion and assess the ranked/`multi_es_fast` search path against fixed relevance and latency cases before changing the default.
- OCTX, Dify, browser folder import, ARM, and Docker-era data migration are excluded.

## Native boundaries

- Preserve `/app/sag` through the fnOS gateway, signed identity, per-tenant UDS workers, package-user privileges, offline vendor wheels, private per-tenant SQLite/LanceDB/uploads, and the existing Public MCP integration.
- Support both existing workspace key formats: numeric UID and optional `uid-name-hash`. Do not move an identity into another tenant's directory or expose physical paths.
- Keep the old 0.7.1 engine and a verified cold backup. Stage a new 0.13.0 engine separately for each tenant; never run the new engine against the old store. Metadata, source names, document originals, settings, conversations, and agents remain accessible. Legacy extracted events/vectors are not served as current evidence; old citations are marked stale.
- An administrator must explicitly accept the disruptive package upgrade before it proceeds. The warning says old knowledge must be reingested and can incur model cost. Missing consent fails closed. Declining does not mutate the old data.
- Each tenant chooses when to reingest. No automatic model call before that action. Reingest uses retained private originals, is resumable/idempotent, and reports missing originals for manual upload. Each tenant sees readiness and progress. Search/QA must not silently treat pending sources as ready.
- Show pending upgrade guidance across authenticated Native pages, with full controls in source details. Keep the last known status and a retry action on status failures; after all legacy documents are ready, show completion in details and remove the global warning. Polling must not discard slow responses or carry status across tenant sessions.
- Old engine data and backups are retained until an administrator manually clears them after checking rebuild and backup. The cleanup must not cross tenant boundaries.

## Proof and delivery

- Test real 0.7.1 fixtures and at least two tenant workspaces: consent, backup and restoration, missing originals, restart/retry, authorization, stale citations, retrieval, spreadsheet records, and no implicit model calls.
- Run the full backend, frontend, i18n, lint, build, and fnOS contract commands in `AGENTS.md`; build the FPK with verified fnpack on Linux x86. Verify archive structure, offline inputs, `/app/sag` base path, package size at most 285 MiB, and SHA-256.
- Deliver the FPK outside the repository with version, commit, checksum, test log, and explicit device-acceptance gap. The existing release workflow currently publishes immediately; candidate builds must not publish automatically.
