"""Keep the runnable Parallel example on SAG's existing MCP adapter path."""

import importlib.util
import json
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

EXAMPLE = Path(__file__).resolve().parents[3] / "examples" / "parallel-search"


def load_smoke():
    spec = importlib.util.spec_from_file_location("parallel_smoke", EXAMPLE / "smoke.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_smoke_uses_mounted_tools_and_reuses_conversation_id(monkeypatch, capsys):
    module = load_smoke()
    calls = []
    specs_seen = []

    class RemoteTool:
        def __init__(self, name):
            self.meta = SimpleNamespace(name=name)

        async def invoke(self, args, context):
            assert context.sources == []
            calls.append((self.meta.name, args))
            return SimpleNamespace(content=json.dumps({"results": [{"url": "https://www.python.org/"}]}))

    @asynccontextmanager
    async def mounted(specs):
        specs_seen.extend(specs)
        yield SimpleNamespace(
            warnings=[],
            tools=[RemoteTool(f"mcp__parallel_search__{name}") for name in ("web_search", "web_fetch")],
        )

    monkeypatch.setattr(module, "open_agent_mcp_tools", mounted)
    await module.smoke("Python documentation", "https://www.python.org/")
    assert specs_seen == [
        ("parallel-search", {
            "name": "parallel-search",
            "url": "https://search.parallel.ai/mcp",
            "headers": {"User-Agent": "SAG/parallel-search-example"},
        }),
    ]
    assert calls[0][1]["search_queries"] == ["Python documentation"]
    assert calls[1][1]["urls"] == ["https://www.python.org/"]
    assert calls[0][1]["session_id"] == calls[1][1]["session_id"]
    assert "https://www.python.org/" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_smoke_reports_connection_failure(monkeypatch):
    module = load_smoke()

    @asynccontextmanager
    async def unavailable(specs):
        yield SimpleNamespace(warnings=[{"code": "mcp_connection_failed"}], tools=[])

    monkeypatch.setattr(module, "open_agent_mcp_tools", unavailable)
    with pytest.raises(RuntimeError, match="connection failed"):
        await module.smoke("Python documentation", "https://www.python.org/")
