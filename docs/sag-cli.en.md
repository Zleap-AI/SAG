# SAG CLI User Guide

`@zleap-ai/sag-cli` is the official command-line client for SAG knowledge bases. From your terminal it lets you:

- Sign in to and validate a running SAG instance;
- Inspect sources and document processing status, and search knowledge directly;
- Verify the local Docker SAG knowledge-base MCP and wire it into Codex and Claude Code with a single command.

## Is this tool for you

- You already have a SAG instance (local Docker or remote HTTP) and want to query, diagnose, and expose it to coding agents from the terminal.
- You are developing or operating SAG and need diagnostics such as `doctor` and `mcp test`.
- You want to use SAG for retrieval inside Codex or Claude Code without hand-writing an MCP config.

## Prerequisites

| Task                                 | Requirement                                                               |
| ------------------------------------ | ------------------------------------------------------------------------- |
| Install and run the CLI              | Node.js **≥ 20.19**                                                       |
| Sign in and search via the HTTP API  | A reachable SAG origin (e.g. `http://localhost:8000`) plus a SAG JWT      |
| Use the local Docker token-less path | Docker CLI, with a SAG API container running `sag_api.mcp.server` locally |
| Wire into Codex                      | Codex CLI (≥ 0.145) installed                                             |
| Wire into Claude Code                | Claude Code (≥ 2.1) installed                                             |

**Where to get the JWT**: sign in to SAG Web → **Settings → Integrations** and copy the JWT. The CLI hides the input and prefers the OS credential store (macOS Keychain, Windows Credential Manager, Linux Secret Service). When that is unavailable the token stays only in the current process; use the `SAG_TOKEN` environment variable in automation.

## Install

```bash
npm install --global @zleap-ai/sag-cli
sag --help
sag version
```

## Quick start: pick the path that fits you

The CLI offers two independent paths. Pick one based on your situation.

### Path A: Local Docker, no token required (recommended for local dev)

Requires a SAG API container running locally in Docker. The CLI discovers the container, talks to it over stdio MCP via `docker exec`, and never needs the JWT.

```bash
# 1. Verify the local Docker SAG MCP works
sag mcp test

# 2. Wire it into an agent (either or both)
sag agent connect codex
sag agent connect claude-code

# 3. Check status any time
sag agent status
```

Scope to a single source with `--source-id`:

```bash
sag mcp test --source-id <source-id>
sag agent connect codex --source-id <source-id>
```

Preview the plan with `--dry-run` before writing anything:

```bash
sag agent connect codex --dry-run
```

Undo an integration:

```bash
sag agent disconnect codex
```

### Path B: HTTP API + JWT (across machines, or no Docker)

Authenticate and search against SAG's HTTP API directly.

```bash
# 1. Register a SAG instance
sag profile add local http://localhost:8000
sag profile use local

# 2. Sign in (prompts for the JWT)
sag auth login
sag auth status

# 3. Health check and browse
sag doctor
sag source list
sag document status --source <source-id>

# 4. Search
sag search "how to wire MCP" --source <source-id> --top-k 5
```

A profile stores only `scheme://host[:port]`. The `/api/v1` prefix is added by the CLI — do not put it in the URL yourself.

## Command reference

```text
sag version
sag auth login | status | logout
sag profile add | list | use | show | remove
sag doctor
sag source list | get | status
sag document list | get | status
sag search <query>
sag mcp test
sag agent list
sag agent connect <codex | claude-code>
sag agent status [codex | claude-code]
sag agent disconnect <codex | claude-code>
```

Common examples:

```bash
sag profile list --json
sag source get <source-id>
sag document list --source <source-id>
sag search "how to wire MCP" --source <source-id> --strategy multi
sag mcp test --container sag-api-1 --timeout 15000
sag agent connect claude-code --name sag-knowledge-local
```

## Global options

```text
--profile <name>   Select a profile
--url <origin>     Override the SAG origin for one command
--json             Emit stable JSON (schema: sag.cli.v1)
--quiet            Print only the essential value
--yes              Confirm a safe local configuration operation
```

## Environment variables and resolution order

Priority: **CLI flag > environment variable > current profile > local default probe**.

```bash
SAG_URL=http://localhost:8000
SAG_TOKEN=<jwt>
SAG_PROFILE=local
```

Profile storage locations:

| Platform | Path                                                                      |
| -------- | ------------------------------------------------------------------------- |
| macOS    | `~/Library/Application Support/sag-cli/config.yaml`                       |
| Linux    | `$XDG_CONFIG_HOME/sag-cli/config.yaml` or `~/.config/sag-cli/config.yaml` |
| Windows  | `%APPDATA%\sag-cli\config.yaml`                                           |

Agent-integration managed state is kept in `managed-connections.yaml` (mode `0600`). Do not edit it by hand.

## Agent integration safety

- SAG CLI **never** writes the JWT into agent configuration. The local Docker path needs no token at all.
- The default MCP entry name is `sag-knowledge-<profile>`, or `sag-knowledge-local` when no profile is active.
- The CLI only removes MCP entries it created whose fingerprint is unchanged. Same-name user entries and external edits block overwrite and delete.
- `--dry-run` shows the plan only; `--yes` skips confirmation but never skips conflict checks.

## JSON output and exit codes

Under `--json`, both success and failure use a fixed schema:

```json
{ "schema": "sag.cli.v1", "ok": true, "data": {} }
```

```json
{
  "schema": "sag.cli.v1",
  "ok": false,
  "error": { "code": "AUTH_REQUIRED", "message": "No SAG token is configured" }
}
```

Tokens never appear in JSON, logs, or error output. Full error and exit codes live in the [CLI architecture docs](https://github.com/Zleap-AI/sag-cli).
