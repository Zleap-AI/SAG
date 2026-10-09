import asyncio
from types import SimpleNamespace

import pytest
from zleap.sag import SagError

from dsh_sag_runtime.protocol import decode_request
from dsh_sag_runtime.server import RuntimeServer, safe_error_data


def request(payload: str):
    return decode_request(payload.encode())


class FakePool:
    def __init__(self) -> None:
        self.started = 0
        self.closed = 0

    async def start(self) -> None:
        self.started += 1

    async def health(self):
        return SimpleNamespace(state="available")

    async def capabilities(self):
        return SimpleNamespace(evidence_read=True)

    async def aclose(self) -> None:
        self.closed += 1


class FakeSearch:
    def __init__(self) -> None:
        self.gate: asyncio.Event | None = None

    async def search(self, params):
        if self.gate is not None:
            await self.gate.wait()
        return {"query": params.query, "evidences": []}


class FakeRead:
    async def read(self, _params):
        return {"title": "t", "content": "c", "offset": 0, "totalChars": 1}


@pytest.mark.asyncio
async def test_server_requires_initialize_and_rejects_duplicate_initialize() -> None:
    pool = FakePool()
    server = RuntimeServer(pool, FakeSearch(), FakeRead(), ("product-docs",), engine_version="0.14.0")

    early = await server.accept(request('{"jsonrpc":"2.0","id":1,"method":"search","params":{"query":"q","namespaces":["product-docs"],"mode":"fast","limit":1}}'))
    ready = await server.accept(request('{"jsonrpc":"2.0","id":2,"method":"initialize","params":{"protocolVersion":"1.0"}}'))
    duplicate = await server.accept(request('{"jsonrpc":"2.0","id":3,"method":"initialize","params":{"protocolVersion":"1.0"}}'))

    assert early.error.data.code == "DSH_SAG_NOT_INITIALIZED"
    assert ready.result == {
        "protocolVersion": "1.0", "engineVersion": "0.14.0", "health": "available",
        "evidenceRead": True, "namespaces": ["product-docs"],
    }
    assert duplicate.error.data.code == "DSH_SAG_ALREADY_INITIALIZED"
    assert pool.started == 1
    await server.aclose()
    assert pool.closed == 1


@pytest.mark.asyncio
async def test_cancel_waits_for_request_to_settle_as_cancelled() -> None:
    search = FakeSearch()
    search.gate = asyncio.Event()
    server = RuntimeServer(FakePool(), search, FakeRead(), ("a",), engine_version="0.14.0")
    await server.accept(request('{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"1.0"}}'))
    running = asyncio.create_task(server.accept(request('{"jsonrpc":"2.0","id":2,"method":"search","params":{"query":"q","namespaces":["a"],"mode":"fast","limit":1}}')))
    await asyncio.sleep(0)

    notification = await server.accept(request('{"jsonrpc":"2.0","method":"$/cancelRequest","params":{"id":2}}'))
    response = await running

    assert notification is None
    assert response.error.data.code == "DSH_SAG_CANCELLED"


@pytest.mark.asyncio
async def test_shutdown_closes_pool_and_rejects_new_work() -> None:
    pool = FakePool()
    server = RuntimeServer(pool, FakeSearch(), FakeRead(), ("a",), engine_version="0.14.0")
    await server.accept(request('{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"1.0"}}'))

    response = await server.accept(request('{"jsonrpc":"2.0","id":2,"method":"shutdown","params":{}}'))
    late = await server.accept(request('{"jsonrpc":"2.0","id":3,"method":"read","params":{"evidenceRef":"a","offset":0,"maxChars":1,"includeEvents":false}}'))

    assert response.result == {}
    assert late.error.data.code == "DSH_SAG_SHUTTING_DOWN"
    assert pool.closed == 1


def test_sag_error_projection_excludes_causes_and_secret_details() -> None:
    error = SagError(
        "provider failed", code="provider_error", operation="search", retryable=True,
        provider="openai", item_id="doc-1", details={"api_key": "secret", "safe": "value"},
        cause=RuntimeError("token=secret"),
    )

    projected = safe_error_data(error)

    assert projected.model_dump(by_alias=True, exclude_none=True) == {
        "code": "provider_error", "operation": "search", "retryable": True,
        "provider": "openai", "itemId": "doc-1", "message": "provider failed",
        "details": {"safe": "value"},
    }
