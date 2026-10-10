# Parallel web search for SAG

[Parallel Search MCP](https://docs.parallel.ai/integrations/mcp/search-mcp) adds
web search and page fetching to SAG's assistant through its existing external
MCP tools. The anonymous endpoint needs no Parallel account or API key. Search
uses Fast mode; free usage has rate limits.

## Connect the assistant

1. Start SAG and configure a tool-capable chat model as usual.
2. Open **Settings → Integrations → External tools**. Paste the contents of
   [`mcp.json`](mcp.json) into the JSON configuration input and click **Mount 1 service**.
3. Enable internet access in the conversation. Ask explicitly to use
   `mcp__parallel_search__web_search` to find a public source, then
   `mcp__parallel_search__web_fetch` to read it when the excerpts are insufficient.

For example: “Use Parallel search to find the official Python asyncio
documentation, then fetch that page and explain how to start a coroutine.
Include the source URL.”

Mounting makes these tools available to the default assistant; the model decides
when to call them. Existing built-in tools and other MCP bindings remain available.
Internet-off conversations skip external MCP services. To disconnect, unmount
`parallel-search` in External tools.

## Run a smoke check without a model

From the repository root, install the backend and run the example:

```bash
python -m venv .venv-parallel
. .venv-parallel/bin/activate
python -m pip install -e ./apps/api
python examples/parallel-search/smoke.py
```

On Windows, activate with `.venv-parallel\Scripts\activate` instead. No additional
dependencies or model credentials are needed for this smoke check. It reads the
same configuration, discovers tools through SAG's MCP loader, and prints search
and fetch results, including URLs and excerpts. It does not change saved settings.

To try another query and page:

```bash
python examples/parallel-search/smoke.py \
  --query "Python TaskGroup documentation" \
  --url https://docs.python.org/3/library/asyncio-task.html
```

The configuration uses Streamable HTTP. It sends a SAG User-Agent and no
Authorization header. Queries and fetched URLs are sent to Parallel, so use
public information. The smoke check exits with an error for connection failures,
missing tools, remote tool errors, or empty results. If a request is rate limited,
wait for the indicated retry time. In the assistant, a failed connection appears
as an unavailable mounted service for that conversation.
