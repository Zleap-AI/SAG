# Using dsh-sag

[简体中文](usage.zh.md) · [Integration overview](../README.md)

`@zleap-ai/dsh-sag` lets DeepSeek Harness search and read your SAG knowledge base, upload files, save text, and manage sources and documents. Its source is maintained in SAG, with independent npm versions and releases.

The default `local` mode connects to a running SAG application. It uses SAG REST operations and checks retrieval capabilities through MCP; no Python runtime is needed in dsh.

## Compatibility

The current dsh-sag **0.2.0** supports DeepSeek Harness **0.2.0-rc.2**.

Prepare Node.js 24 and pnpm 11.7.0, with `dsh` and `pnpm` on PATH. SAG must include **Settings → Integrations → Connect dsh** and the local connector API.

## Install and start

Start SAG first. To install the supported dsh host, run the following command.

```sh
npm install --global @deepseek-ai/dsh@0.2.0-rc.2
```

Install the plugin from npm. The following commands apply after 0.2.0 is published to npm.

```sh
dsh plugin --profile web add @zleap-ai/dsh-sag@0.2.0
dsh plugin --profile web exec dsh-sag doctor
dsh --profile web
```

Once `doctor` reports `SAG 已连接`, use SAG in the dsh conversation. Local discovery usually connects automatically without manual credentials.

## Save a connection

If discovery fails, choose one of these routes.

```sh
# Discover a running local SAG
dsh plugin --profile web exec dsh-sag setup

# Use the file exported from SAG's Connect dsh settings
dsh plugin --profile web exec dsh-sag setup ./sag-dsh.json

# Specify the local SAG address
dsh plugin --profile web exec dsh-sag setup --url http://127.0.0.1:8000
```

For the exported-file route, run the command in that file's directory or replace `./sag-dsh.json` with its actual path. Run `dsh plugin --profile web exec dsh-sag doctor`, then start or restart dsh.

Use the same profile and `DSH_HOME` throughout. For a custom profile, replace `web` consistently in every command.

## Tools in local mode

The plugin registers these 11 tools. Knowledge-base and document operations check the capabilities advertised by the connected SAG before calling their endpoints.

| Tool | What it does |
| --- | --- |
| `sag_status` | Check the SAG connection and report available capabilities. |
| `sag_list_sources` | List knowledge bases and their stable IDs. |
| `sag_create_source` | Create a knowledge base for uploaded files or text. |
| `sag_search` | Search knowledge bases and return evidence references. |
| `sag_read` | Read a bounded page of the source chunk identified by a search `evidence_ref`. |
| `sag_list_documents` | List documents and their processing states in a knowledge base. |
| `sag_get_document` | Inspect one document and its current processing state. |
| `sag_reprocess_document` | Queue document reprocessing and return the accepted job state. |
| `sag_delete_document` | Delete a document after dsh user approval. |
| `sag_upload_file` | Upload one local file allowed by SAG and return its processing state. |
| `sag_ingest_text` | Save text as a document and return its processing state. |

Upload, text ingestion and reprocessing run asynchronously. Use document inspection to check progress, and search the document once its status is `ready`. SAG declares the allowed file types; file size must satisfy both SAG and plugin limits.

For uploads, text ingestion and document operations, specify the target knowledge base when more than one exists and no default is configured. Use `sag_search` before `sag_read`; pass the returned `evidence_ref` unchanged and continue reading with the returned next offset.

## Ask dsh naturally

- “Search SAG for the upload limit and cite the original text.”
- “Upload `./product-manual.pdf` to the Product docs knowledge base, then summarize it when processing is ready.”
- “Save the following meeting decisions as a note in SAG.”
- “List documents in Product docs and reprocess the failed document.”

dsh uses the registered tools to carry out these tasks. Document deletion prompts for user approval.

## Embedded mode

Advanced deployments can let dsh manage a Python sidecar running exactly `zleap-sag==0.14.0`. This mode registers only `sag_search` and `sag_read` for the configured namespaces.

Prepare Python 3.11+, uv, the engine environment and namespace configuration using the [embedded guide](../packages/dsh-sag/docs/embedded.md).

## Source installation for development

To develop or debug plugin changes, build a local package from the root of your SAG checkout.

```sh
cd integrations/dsh-sag
pnpm install --frozen-lockfile
pnpm run pack
dsh plugin --profile web add ./artifacts/zleap-ai-dsh-sag-0.2.0.tgz
```

Then run `dsh plugin --profile web exec dsh-sag doctor` and start or restart that profile.

See the [package user guide](../packages/dsh-sag/README.md) for upgrade and recovery, and the [independent release guide](releasing.md) for verification and npm publication.
