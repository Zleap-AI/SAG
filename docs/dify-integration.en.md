# Use SAG as a Dify External Knowledge Base

SAG implements Dify's external knowledge base retrieval protocol. SAG continues to handle document upload, parsing, chunking, embeddings, and event–entity extraction. At application runtime, Dify only asks SAG for retrieval evidence. This integration is entirely optional: it does not replace or change any other knowledge base capability in either Dify or SAG.

## Prerequisites

- Both SAG and Dify are running with Docker Compose.
- Documents in the target SAG source have finished processing.
- Dify containers can reach the SAG API over a shared Docker network.
- This integration follows SAG's single-user boundary: one Dify external knowledge base is bound to one SAG source.

## 1. Initialize the SAG Dify API key

From the root of the SAG repository, run:

```bash
make setup-dify
```

The command generates a cryptographically strong key, stores it in the Git-ignored root `.env` file, and prints:

```text
Endpoint: http://sag:8000/api/v1/dify
API Key: <generated key>
```

An existing non-empty `SAG_DIFY_API_KEY` is never overwritten. If you do not run this command or configure the key manually, the Dify-compatible endpoint returns `503`. SAG's default startup and all other functionality remain unaffected.

## 2. Connect the SAG API to Dify's Docker network

First, identify Dify's Docker network from Dify's `docker` directory:

```bash
docker compose ps
docker network ls
```

When started from the default directory name, Dify commonly uses `docker_default`. From the SAG repository root, start SAG with the optional Compose override:

```bash
DIFY_NETWORK_NAME=docker_default \
docker compose -f compose.yaml -f compose.dify.yaml up -d --build
```

This adds only SAG's `api` service to the selected network and gives it the network alias `sag`. Do not expose the Dify-compatible endpoint directly to the public internet.

## 3. Configure Dify's container access policy

Dify sends external knowledge base requests through its SSRF Proxy. Add the following settings to Dify's `docker/.env`:

```dotenv
SSRF_PROXY_ALLOW_PRIVATE_DOMAINS=sag
SSRF_DEFAULT_TIME_OUT=30
SSRF_DEFAULT_READ_TIME_OUT=30
```

Both `SSRF_DEFAULT_TIME_OUT` and `SSRF_DEFAULT_READ_TIME_OUT` must exceed SAG's per-source retrieval timeout and leave room for network overhead. Dify applies the read timeout independently, so increasing only the overall timeout can still cause a request to be interrupted after the default five seconds.

With the default `multi → vector` fallback enabled, one request can consume up to two consecutive per-source timeouts. Set both Dify timeouts to more than `2 × SAG_SEARCH_SOURCE_TIMEOUT + network overhead`. For example, when SAG's default per-source timeout is 12 seconds, 30 seconds is appropriate. If it is increased to 45 seconds, configure Dify for at least 100 seconds.

Recreate the relevant Dify services for the configuration to take effect:

```bash
docker compose up -d --force-recreate api ssrf_proxy
```

These changes apply only to your Dify deployment; no Dify source-code changes are required.

## 4. Find the SAG Source ID

Open the target source in SAG. Its URL follows this pattern:

```text
http://localhost:3000/knowledge/<SOURCE_ID>
```

The final segment, `<SOURCE_ID>`, is the Knowledge ID to enter in Dify. You can also call `GET /api/v1/sources` with a SAG user JWT to retrieve sources and their `id` values.

## 5. Create the external knowledge base in Dify

1. Go to **Knowledge → Create Knowledge Base → Connect to an External Knowledge Base**.
2. Create an external knowledge API:
   - **Name:** for example, `SAG`
   - **Endpoint:** `http://sag:8000/api/v1/dify`
   - **API Key:** the key printed by `make setup-dify`
3. Create the external knowledge base and enter the target SAG `source.id` as the **Knowledge ID**.
4. Run Dify's retrieval test, or add a Knowledge Retrieval node to an application to verify the results and citations.

Dify automatically appends `/retrieval` to the Endpoint. Do not include `/retrieval` in the Endpoint yourself, and do not mistakenly use HTTPS for the container-internal HTTP address.

## Retrieval protocol

Dify ultimately calls:

```http
POST /api/v1/dify/retrieval
Authorization: Bearer <SAG_DIFY_API_KEY>
Content-Type: application/json
```

Example request:

```json
{
  "knowledge_id": "<SAG_SOURCE_ID>",
  "query": "What is the identifier for the Stellar Project?",
  "retrieval_setting": {
    "top_k": 5,
    "score_threshold": 0.3
  }
}
```

The compatible endpoint always uses SAG's `multi` retrieval strategy and retains SAG's existing fallback to `vector` when a source retrieval times out, fails, or returns no results. Each returned `record` includes `source_id`, `source_name`, `chunk_id`, `heading`, and `document_id` for citation traceability in Dify.

## Current limitations

- The initial release does not support `metadata_condition`. Requests that include it return `422`; it is never silently ignored.
- One Dify external knowledge base can be bound to only one SAG `source.id`.
- SAG is currently a single-user product. Do not treat this endpoint as a cross-tenant knowledge-isolation boundary.
- This endpoint supplies retrieval evidence only. It does not upload, update, or delete SAG documents from Dify.
