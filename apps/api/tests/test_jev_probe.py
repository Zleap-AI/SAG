"""Line-level scoring probes use the SDK protocol and release real HTTP sockets."""

import asyncio
import json

import pytest
from aiohttp import web

from sag_api.core.config import Settings
from sag_api.sag.jev_probe import probe_jev_connection


@pytest.fixture
async def scoring_server():
    runners = []
    releases = []

    async def start(*, status=200, response=None, stall=False):
        observations = []
        started = asyncio.Event()
        release = asyncio.Event()
        releases.append(release)

        async def handler(request):
            payload = await request.json()
            observations.append({
                "path": request.path_qs, "authorization": request.headers.get("Authorization"),
                "payload": payload, "transport": request.transport,
            })
            started.set()
            if stall:
                await release.wait()
            body = response if response is not None else {
                "model": payload["model"], "usage": {"input_tokens": 10, "output_tokens": 2},
                "answers": {key: 0.8 for key in payload["questions"]},
            }
            return web.json_response(body, status=status)

        app = web.Application()
        app.router.add_post("/custom/questions", handler)
        runner = web.AppRunner(app, shutdown_timeout=0.1)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        runners.append(runner)
        port = site._server.sockets[0].getsockname()[1]
        return f"http://127.0.0.1:{port}/custom/questions?version=1", observations, started

    try:
        yield start
    finally:
        for release in releases:
            release.set()
        for runner in runners:
            await runner.cleanup()


async def assert_socket_closed(observation):
    for _ in range(20):
        if observation["transport"].is_closing():
            return
        await asyncio.sleep(0.01)
    assert observation["transport"].is_closing(), "probe left a real scorer socket open"


@pytest.mark.asyncio
async def test_probe_uses_real_scorer_auth_model_full_url_and_synthetic_evidence(scoring_server):
    endpoint, requests, _ = await scoring_server()
    configuration = Settings(
        _env_file=None, jev_endpoint=endpoint, jev_api_key="independent-key", jev_model="model-test",
    )
    result = await probe_jev_connection(configuration)
    assert result.ok is True
    assert len(requests) == 1
    assert requests[0]["authorization"] == "Bearer independent-key"
    assert requests[0]["path"] == "/custom/questions?version=1"
    assert requests[0]["payload"]["model"] == "model-test"
    assert "Harbor City" in json.dumps(requests[0]["payload"])
    assert "independent-key" not in result.model_dump_json()
    await assert_socket_closed(requests[0])


@pytest.mark.asyncio
async def test_probe_requires_independent_key_before_network_even_with_llm_key(scoring_server):
    endpoint, requests, _ = await scoring_server()
    result = await probe_jev_connection(Settings(_env_file=None, jev_endpoint=endpoint, llm_api_key="llm-key"))
    assert result.ok is False and "独立" in result.message and "Key" in result.message
    assert requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize(("status", "message"), [(401, "凭据"), (400, "协议"), (503, "暂时不可用")])
async def test_probe_errors_do_not_leak_provider_body_or_retry(scoring_server, status, message, caplog):
    endpoint, requests, _ = await scoring_server(status=status, response={"error": "private-response-secret"})
    result = await probe_jev_connection(Settings(_env_file=None, jev_endpoint=endpoint, jev_api_key="private-key"))
    assert result.ok is False and message in result.message
    assert len(requests) == 1
    for secret in ("private-response-secret", "private-key", endpoint):
        assert secret not in result.model_dump_json()
        assert secret not in caplog.text
    await assert_socket_closed(requests[0])


@pytest.mark.asyncio
async def test_probe_rejects_invalid_scoring_protocol_without_exposing_body(scoring_server):
    endpoint, requests, _ = await scoring_server(response={"error": "private-provider-detail"})
    result = await probe_jev_connection(Settings(_env_file=None, jev_endpoint=endpoint, jev_api_key="key"))
    assert result.ok is False
    assert "private-provider-detail" not in result.model_dump_json()
    await assert_socket_closed(requests[0])


@pytest.mark.asyncio
async def test_probe_timeout_closes_real_socket(scoring_server):
    endpoint, requests, _ = await scoring_server(stall=True)
    result = await asyncio.wait_for(probe_jev_connection(Settings(
        _env_file=None, jev_endpoint=endpoint, jev_api_key="key", jev_ranking_timeout_seconds=0.1,
    )), timeout=2)
    assert result.ok is False and "超时" in result.message
    assert len(requests) == 1
    await assert_socket_closed(requests[0])


@pytest.mark.asyncio
async def test_probe_caller_cancellation_propagates_and_closes_real_socket(scoring_server):
    endpoint, requests, started = await scoring_server(stall=True)
    task = asyncio.create_task(probe_jev_connection(Settings(
        _env_file=None, jev_endpoint=endpoint, jev_api_key="key",
    )))
    await asyncio.wait_for(started.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await assert_socket_closed(requests[0])
