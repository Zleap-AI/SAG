"""Search and fetch anonymously through SAG's maintained MCP tool adapter."""

from __future__ import annotations

import argparse
import asyncio
import json
import uuid
from pathlib import Path

from sag_api.tools.base import ToolContext
from sag_api.tools.mcp import open_agent_mcp_tools


async def smoke(query: str, url: str) -> None:
    config_path = Path(__file__).with_name("mcp.json")
    servers = json.loads(config_path.read_text(encoding="utf-8"))["mcpServers"]
    specs = [(name, {"name": name, **config}) for name, config in servers.items()]
    context = ToolContext(engine_manager=None, sources=[])
    session_id = str(uuid.uuid4())
    async with open_agent_mcp_tools(specs) as bundle:
        if bundle.warnings:
            raise RuntimeError("Parallel MCP connection failed; check connectivity and try again later.")
        tools = {tool.meta.name: tool for tool in bundle.tools}
        calls = [
            (
                "mcp__parallel_search__web_search",
                {"objective": query, "search_queries": [query], "session_id": session_id},
            ),
            (
                "mcp__parallel_search__web_fetch",
                {"urls": [url], "objective": query[:200], "session_id": session_id},
            ),
        ]
        for name, arguments in calls:
            if name not in tools:
                raise RuntimeError(f"Parallel MCP did not expose the expected tool: {name}")
            result = await tools[name].invoke(arguments, context)
            payload = json.loads(result.content)
            if not payload.get("results"):
                raise RuntimeError(f"{name} returned no results: {result.content}")
            print(f"\n{name}\n{result.content}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--query", default="Python asyncio documentation")
    parser.add_argument("--url", default="https://docs.python.org/3/library/asyncio.html")
    args = parser.parse_args()
    asyncio.run(smoke(args.query, args.url))


if __name__ == "__main__":
    main()
