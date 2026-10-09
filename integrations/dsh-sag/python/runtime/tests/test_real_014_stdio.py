"""Published engine and stdio contract, with deterministic HTTP model responses.

The engine, OpenAI clients, SQLite, LanceDB and sidecar process are real. The
local provider verifies integration structure rather than external model quality.
"""

import asyncio
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import sys

from aiohttp import web
import pytest
from zleap.sag import DataEngine, EngineConfig
from zleap.sag.config import EmbeddingConfig, LLMConfig
from zleap.sag.operations import OperationContext, ProcessSourceRequest
from zleap.sag.pipeline import ArticleSource, ExtractionOptions, SourceDescriptor

CONTENT = "# 上传说明\n\nDW-2412P30 单文件上传上限为 100 MiB。🙂"


class LocalProvider:
    def __init__(self):
        self.blocked = asyncio.Event()
        self.release = asyncio.Event()
        self.embedding_requests = []
        self.chat_requests = []

    async def embeddings(self, request):
        body = await request.json()
        self.embedding_requests.append(body)
        inputs = body["input"]
        inputs = [inputs] if isinstance(inputs, str) else inputs
        if inputs == ["cancel-this-query"]:
            self.blocked.set()
            await self.release.wait()
        return web.json_response({
            "object": "list", "model": body["model"],
            "data": [{"object": "embedding", "index": index, "embedding": [0.5] * 32} for index, _ in enumerate(inputs)],
            "usage": {"prompt_tokens": 1, "total_tokens": 1},
        })

    async def chat(self, request):
        body = await request.json()
        self.chat_requests.append(body)
        properties = body["response_format"]["json_schema"]["schema"].get("properties", {})
        if "named_entities" in properties:
            result = {"named_entities": ["DW-2412P30"]}
        elif "useful_relations" in properties:
            result = {"useful_relations": [1]}
        else:
            result = {"type": "response", "data": {"items": [{
                "title": "单文件上传限制", "content": "DW-2412P30 的上传上限是 100 MiB。",
                "entities": [{"type": "product", "name": "DW-2412P30", "description": "产品型号"}],
                "is_valid": True, "children": [],
            }]}}
        return web.json_response({
            "id": "local-completion", "object": "chat.completion", "created": 0, "model": body["model"],
            "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": json.dumps(result)}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        })


async def send(process, request_id, method, params, diagnostics=None):
    process.stdin.write((json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}) + "\n").encode())
    await process.stdin.drain()
    frame = await asyncio.wait_for(process.stdout.readline(), 20)
    if not frame:
        await asyncio.wait_for(process.wait(), 5)
        detail = (await diagnostics).decode() if diagnostics is not None else f"exit {process.returncode}"
        raise AssertionError(f"sidecar exited before replying: {detail}")
    return json.loads(frame)


@pytest.mark.asyncio
async def test_published_014_sidecar_starts_searches_reads_cancels_and_restarts(tmp_path):
    assert importlib.metadata.version("zleap-sag") == "0.14.0"
    provider = LocalProvider()
    application = web.Application()
    application.router.add_post("/v1/embeddings", provider.embeddings)
    application.router.add_post("/v1/chat/completions", provider.chat)
    runner = web.AppRunner(application)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    address = runner.addresses[0]
    base_url = f"http://127.0.0.1:{address[1]}/v1"
    data_dir = tmp_path / "data"
    config = EngineConfig(
        storage_mode="normal", data_dir=str(data_dir),
        llm=LLMConfig(api_key="local-test", base_url=base_url, model="local-test", max_retries=0),
        embedding=EmbeddingConfig(model="local-test", schema_dimensions=32, request_dimensions=None, max_retries=0),
    )
    env_file = tmp_path / "sag.env"
    env_file.write_text(
        f"SAG_STORAGE_MODE=normal\nSAG_DATA_DIR={data_dir}\nOPENAI_API_KEY=local-test\n"
        f"OPENAI_BASE_URL={base_url}\nLLM_MODEL=local-test\nEMBEDDING_MODEL=local-test\n"
        "EMBEDDING_SCHEMA_DIMENSIONS=32\nEMBEDDING_REQUEST_DIMENSIONS=none\nLOG_LEVEL=ERROR\n"
    )
    runtime_src = str(Path(__file__).parents[1] / "src")
    process_env = {key: value for key, value in os.environ.items() if key in {"PATH", "SYSTEMROOT", "TEMP", "TMP", "SSL_CERT_FILE"}}
    process_env.update(PYTHONPATH=runtime_src, PYTHONUNBUFFERED="1")
    process = None
    diagnostics = None
    try:
        async with DataEngine(config, data_source_id="product-docs") as writer:
            result = await writer.process_source(ProcessSourceRequest(
                context=OperationContext(
                    operation_id="stdio-seed", idempotency_key="stdio-seed",
                    request_digest=hashlib.sha256(CONTENT.encode()).hexdigest(), fence_scope="product-docs",
                    fence_token=1, owner_id="test", lease_seconds=30,
                ),
                source=ArticleSource(content=CONTENT, descriptor=SourceDescriptor(
                    data_source_id="product-docs", source_id="manual", source_type="article", title="上传说明",
                )),
                extraction_options=ExtractionOptions(contract="minimal"),
            ))
            assert result.status == "succeeded"
            assert (await writer.schema_status()).actual_version == 4

        for restart in range(2):
            process = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "dsh_sag_runtime", "--env-file", str(env_file), "--namespace", "product-docs",
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                cwd=tmp_path, env=process_env,
            )
            diagnostics = asyncio.create_task(process.stderr.read())
            ready = await send(process, 1, "initialize", {"protocolVersion": "1.0"}, diagnostics)
            assert ready["result"] == {
                "protocolVersion": "1.0", "engineVersion": "0.14.0", "health": "available",
                "evidenceRead": True, "namespaces": ["product-docs"],
            }
            for mode in ("fast", "precise"):
                found = await send(process, 2, "search", {"query": "DW-2412P30 上传限制", "namespaces": ["product-docs"], "mode": mode, "limit": 5})
                evidence = found["result"]["evidences"][0]
                assert evidence["namespaceId"] == "product-docs"
                assert evidence["sourceId"] == "manual"
                assert "100 MiB" in evidence["excerpt"]
                offset = 0
                pages = []
                while True:
                    page = (await send(process, 3, "read", {"evidenceRef": evidence["evidenceRef"], "offset": offset, "maxChars": 7, "includeEvents": True}))["result"]
                    assert page["offset"] == offset
                    assert page["events"]
                    pages.append(page["content"])
                    if "nextOffset" not in page:
                        break
                    offset = page["nextOffset"]
                assert "".join(pages) == CONTENT

            if restart == 0:
                process.stdin.write(b'{"jsonrpc":"2.0","id":4,"method":"search","params":{"query":"cancel-this-query","namespaces":["product-docs"],"mode":"fast","limit":1}}\n')
                await process.stdin.drain()
                await asyncio.wait_for(provider.blocked.wait(), 5)
                process.stdin.write(b'{"jsonrpc":"2.0","method":"$/cancelRequest","params":{"id":4}}\n')
                await process.stdin.drain()
                cancelled = json.loads(await asyncio.wait_for(process.stdout.readline(), 5))
                assert cancelled["id"] == 4
                assert cancelled["error"]["data"]["code"] == "DSH_SAG_CANCELLED"
                provider.release.set()
            assert (await send(process, 5, "shutdown", {}))["result"] == {}
            process.stdin.close()
            assert await asyncio.wait_for(process.wait(), 10) == 0
            assert await process.stdout.read() == b""
            await diagnostics
            process = None

        assert provider.chat_requests
        assert provider.embedding_requests
        assert all("dimensions" not in body for body in provider.embedding_requests)
    finally:
        provider.release.set()
        if process is not None and process.returncode is None:
            process.kill()
            await process.wait()
        if diagnostics is not None:
            await diagnostics
        await runner.cleanup()


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy_dimensions", ["", "32"])
async def test_legacy_dimension_setting_fails_before_opening_storage(tmp_path, legacy_dimensions):
    """An explicit removed setting must not silently pick a new vector space."""
    data_dir = tmp_path / "untouched"
    env_file = tmp_path / "sag.env"
    env_file.write_text(
        f"SAG_STORAGE_MODE=normal\nSAG_DATA_DIR={data_dir}\nOPENAI_API_KEY=local-test\n"
        f"LLM_MODEL=local-test\nEMBEDDING_MODEL=local-test\nEMBEDDING_DIMENSIONS={legacy_dimensions}\n"
    )
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "dsh_sag_runtime", "--env-file", str(env_file), "--namespace", "product-docs",
        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        cwd=tmp_path, env={"PYTHONPATH": str(Path(__file__).parents[1] / "src")},
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), 10)
        assert process.returncode == 1
        assert stdout == b""
        assert "EMBEDDING_SCHEMA_DIMENSIONS" in stderr.decode()
        assert "EMBEDDING_REQUEST_DIMENSIONS" in stderr.decode()
        assert not data_dir.exists()
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
