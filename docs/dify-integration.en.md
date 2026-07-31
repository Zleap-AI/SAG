# Use SAG as a Dify External Knowledge Base

Dify calls SAG only for retrieval evidence at runtime. Uploading, parsing, chunking, and embedding remain in SAG.

~~~text
Dify API → SSRF Proxy → sag:8000 on the Docker network → SAG /api/v1/dify/retrieval
~~~

One Dify external knowledge base maps to one SAG source (source.id).

## 1. Configure SAG

Set these values in the SAG repository root .env file:

~~~dotenv
# Dedicated key for Dify. Dify must use the same value.
SAG_DIFY_API_KEY=replace-with-a-strong-random-key

# Dify-only retrieval strategy:
# vector (default): low latency; no SAG internal LLM reranking.
# multi: entity expansion plus SAG internal LLM reranking; higher latency.
SAG_DIFY_SEARCH_STRATEGY=vector
~~~

This setting affects the Dify endpoint only. It does not change the retrieval strategy in SAG Web.

### Verify: values reached the API container

~~~bash
docker exec sag-api-1 sh -lc 'python - <<'"'"'PY'"'"'
import os
print("dify_key_configured=", bool(os.getenv("SAG_DIFY_API_KEY")))
print("dify_search_strategy=", os.getenv("SAG_DIFY_SEARCH_STRATEGY"))
PY'
~~~

Expected output:

~~~text
dify_key_configured= True
dify_search_strategy= vector
~~~

## 2. Attach SAG API to the Dify Docker network

Find the Dify network name:

~~~bash
docker network ls
~~~

The default is usually docker_default. From the SAG repository root:

~~~bash
DIFY_NETWORK_NAME=docker_default \
docker compose -f compose.yaml -f compose.dify.yaml up -d --build --force-recreate --no-deps api
~~~

This rebuilds only SAG API; it does not stop SAG Web, Dify, or databases.

### Verify: network and health

~~~bash
docker inspect sag-api-1 \
  --format '{{range $name, $network := .NetworkSettings.Networks}}{{println $name}}{{end}}'

docker run --rm --network docker_default busybox:latest \
  wget -S -O - -T 10 http://sag:8000/api/v1/system/ready
~~~

The network output must include sag_default and docker_default. The health request must return HTTP 200 and:

~~~json
{"status":"ready","db":true}
~~~

sag:8000 is a Docker-internal address. Use docker ps to find the host port.

## 3. Configure Dify SSRF access

Edit Dify docker/.env:

~~~dotenv
SSRF_PROXY_ALLOW_PRIVATE_DOMAINS=sag
SSRF_DEFAULT_TIME_OUT=30
SSRF_DEFAULT_READ_TIME_OUT=30
~~~

- SSRF_PROXY_ALLOW_PRIVATE_DOMAINS permits the private Docker hostname sag.
- SSRF_DEFAULT_TIME_OUT is the maximum total request time in seconds.
- SSRF_DEFAULT_READ_TIME_OUT is the maximum response-read time after connecting.

Thirty seconds is normally enough for vector. When using multi, increase both values according to SAG's per-source timeout and LLM response time.

Recreate the related Dify services from its docker directory:

~~~bash
docker compose up -d --force-recreate api ssrf_proxy
~~~

### Verify: Dify received the settings

~~~bash
docker exec docker-ssrf_proxy-1 sh -lc \
  'env | grep "^SSRF_PROXY_ALLOW_PRIVATE_DOMAINS="'

docker exec docker-api-1 sh -lc \
  'env | grep -E "^(SSRF_DEFAULT_TIME_OUT|SSRF_DEFAULT_READ_TIME_OUT)="'
~~~

Expected output:

~~~text
SSRF_PROXY_ALLOW_PRIVATE_DOMAINS=sag
SSRF_DEFAULT_TIME_OUT=30
SSRF_DEFAULT_READ_TIME_OUT=30
~~~

## 4. Bind a source in Dify

1. Prepare a processed source in SAG Web and copy its complete source.id.
2. In Dify, open **Knowledge → Connect to an External Knowledge Base** and create an external API:
   - Endpoint: http://sag:8000/api/v1/dify
   - API Key: the complete SAG_DIFY_API_KEY value
3. Create the external knowledge base and set Knowledge ID to the complete source.id.

Do not append /retrieval; Dify does it automatically. Do not use localhost or HTTPS.

### Verify: endpoint authentication

From the SAG repository root:

~~~bash
KEY=$(sed -n 's/^SAG_DIFY_API_KEY=//p' .env)

curl -i -sS -X POST \
  -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  http://127.0.0.1:8001/api/v1/dify/retrieval \
  -d '{"knowledge_id":"","query":""}'
~~~

Expect HTTP 200 and:

~~~json
{"records":[]}
~~~

Replace 8001 with the host port shown for SAG API by docker ps. If Dify displays an SSRF error but ssrf_proxy shows HIER_DIRECT/... TCP_MISS/403, first verify that Dify's saved API key matches SAG .env.

## 5. Verify the strategy

Run a Dify hit test. Each returned record includes metadata such as:

~~~json
{
  "retrieval_strategy": "vector",
  "fallback_used": false
}
~~~

- `retrieval_strategy` is the strategy actually used. When a `multi` request is downgraded inside SAG (timeout, failure, or empty result), this appears as `vector`.
- `fallback_used` is `true` when `multi` fell back to `vector`.

## Strategy reference

| Strategy | SAG internal LLM | Latency | Best for |
| --- | --- | --- | --- |
| vector (default) | No LLM reranking | Low | Normal low-latency Dify Q&A |
| multi | Entity extraction and reranking use SAG's configured LLM | High | Higher-recall cases that can accept longer waits |

Dify's application LLM still produces the final natural-language answer. This setting controls only the evidence retrieved by SAG.

