"""自部署 MinerU 4.x V1 API 适配：上传 → 提交任务 → 轮询 → 下载 zip。

模拟响应按 MinerU 4.0.10 `mineru-kit api-server` 的真实返回结构编写。
"""

from __future__ import annotations

import io
import json
import zipfile
from typing import Any

import httpx
import pytest

from sag_api.core.config import Settings
from sag_api.core.errors import ConfigurationError, ServiceUnavailableError, UpstreamError
from sag_api.parsing import service
from sag_api.parsing.mineru import MinerUClient, ParsePaused, _markdown_from_zip
from sag_api.parsing.mineru_v1 import SelfHostedMinerUClient
from sag_api.services import settings_service

BASE = "http://127.0.0.1:18000"
API_KEY = "self-hosted-key"


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "mineru_provider": "self_hosted",
        "mineru_base_url": BASE,
        "mineru_api_key": API_KEY,
        "mineru_tier": "flash",
        "mineru_allow_private_base_url": True,
        "mineru_poll_interval": 0.001,
        "mineru_poll_timeout": 1,
    }
    values.update(overrides)
    return Settings(
        _env_file=None,
        data_dir="/tmp/sag-test-engine",
        upload_dir="/tmp/sag-test-uploads",
        **values,
    )


def _v1_zip(markdown: str) -> bytes:
    target = io.BytesIO()
    with zipfile.ZipFile(target, "w") as archive:
        archive.writestr("markdown.md", markdown)
        archive.writestr("middle_json.json", "{}")
        archive.writestr("structured_content.json", "{}")
    return target.getvalue()


def _job(job_id: str, status: str, file_status: str, **file_fields: Any) -> dict[str, Any]:
    return {
        "job_id": job_id,
        "status": status,
        "created_at": "2026-10-02T08:03:31Z",
        "started_at": None,
        "finished_at": None,
        "tier": "flash",
        "output_formats": ["zip"],
        "access_level": "registered",
        "progress": {"completed": 0, "failed": 0, "total": 1},
        "files": [
            {
                "file_id": "file-src",
                "name": "paper.pdf",
                "page_range": "",
                "status": file_status,
                "parse": None,
                "output_files": None,
                "error": None,
                **file_fields,
            }
        ],
        "links": {"self": f"/v1/parse/jobs/{job_id}", "cancel": f"/v1/parse/jobs/{job_id}"},
    }


def _completed_job(job_id: str = "job_1") -> dict[str, Any]:
    outputs = {
        "markdown": None,
        "middle_json": None,
        "structured_content": None,
        "html": None,
        "latex": None,
        "docx": None,
        "zip": {"file_id": "file-zip", "bytes": 100},
    }
    return _job(job_id, "completed", "completed", output_files=outputs)


class FakeV1Server:
    """有状态的 V1 服务模拟；按需替换某一步的响应。"""

    def __init__(self, base: str = BASE, *, markdown: str = "# Self hosted result") -> None:
        self.base = base
        self.markdown = markdown
        self.requests: list[httpx.Request] = []
        self.upload_url: str | None = None
        self.polls: list[dict[str, Any]] = [_job("job_1", "running", "running"), _completed_job()]
        self.overrides: dict[tuple[str, str], Any] = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        key = (request.method, request.url.path)
        if key in self.overrides:
            override = self.overrides[key]
            return override(request) if callable(override) else override
        if key == ("POST", "/v1/uploads"):
            return httpx.Response(
                200,
                json={
                    "id": "upload_1",
                    "object": "upload",
                    "status": "pending",
                    "upload_url": self.upload_url or f"{self.base}/v1/uploads/upload_1/content",
                    "upload_method": "PUT",
                    "upload_headers": {"Content-Type": "application/pdf"},
                    "file": None,
                },
            )
        if request.method == "PUT":
            return httpx.Response(200)
        if key == ("POST", "/v1/uploads/upload_1/complete"):
            return httpx.Response(
                200,
                json={"id": "upload_1", "status": "completed", "file": {"id": "file-src"}},
            )
        if key == ("POST", "/v1/parse/jobs"):
            return httpx.Response(202, json=_job("job_1", "queued", "queued"))
        if key == ("GET", "/v1/parse/jobs/job_1"):
            payload = self.polls.pop(0) if len(self.polls) > 1 else self.polls[0]
            return httpx.Response(200, json=payload)
        if key == ("GET", "/v1/files/file-zip/content"):
            return httpx.Response(
                200,
                content=_v1_zip(self.markdown),
                headers={"content-type": "application/octet-stream"},
            )
        return httpx.Response(
            404,
            json={"error": {"type": "invalid_request_error", "code": "not_found", "message": "missing"}},
        )

    def calls(self) -> list[tuple[str, str]]:
        return [(request.method, str(request.url)) for request in self.requests]


@pytest.fixture
def v1_server(monkeypatch):
    server = FakeV1Server()
    real_client = httpx.AsyncClient

    def client_factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(server)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client_factory)
    return server


@pytest.fixture
def pdf(tmp_path):
    path = tmp_path / "paper.pdf"
    path.write_bytes(b"%PDF-1.4 self hosted")
    return path


async def _collect(target: list[dict[str, Any]], state: dict[str, Any]) -> None:
    target.append(dict(state))


@pytest.mark.asyncio
async def test_self_hosted_v1_full_flow(v1_server, pdf):
    settings = _settings(mineru_base_url=f"{BASE}/v1/")
    client = MinerUClient(settings)
    states: list[dict[str, Any]] = []

    assert isinstance(client, SelfHostedMinerUClient)
    assert client.signature == service._signature("mineru", settings) == "mineru-v1-flash-auto"

    markdown = await client.parse(str(pdf), on_state=lambda state: _collect(states, state))

    assert markdown == "# Self hosted result\n"
    assert v1_server.calls() == [
        ("POST", f"{BASE}/v1/uploads"),
        ("PUT", f"{BASE}/v1/uploads/upload_1/content"),
        ("POST", f"{BASE}/v1/uploads/upload_1/complete"),
        ("POST", f"{BASE}/v1/parse/jobs"),
        ("GET", f"{BASE}/v1/parse/jobs/job_1"),
        ("GET", f"{BASE}/v1/parse/jobs/job_1"),
        ("GET", f"{BASE}/v1/files/file-zip/content"),
    ]
    assert all(r.headers["authorization"] == f"Bearer {API_KEY}" for r in v1_server.requests)
    create_upload = json.loads(v1_server.requests[0].content)
    assert create_upload["filename"] == "paper.pdf"
    assert create_upload["bytes"] == pdf.stat().st_size
    assert create_upload["mime_type"] == "application/pdf"
    assert create_upload["purpose"] == "parse"
    assert len(create_upload["sha256sum"]) == 64
    assert v1_server.requests[1].headers["content-type"] == "application/pdf"
    assert v1_server.requests[1].read() == pdf.read_bytes()
    assert json.loads(v1_server.requests[3].content) == {
        "files": [{"source": {"type": "file_id", "file_id": "file-src"}}],
        "ocr_mode": "auto",
        "output_formats": ["zip"],
        "tier": "flash",
    }
    assert states[-1]["status"] == "done"
    assert states[-1]["mineru_service"] == "self_hosted"
    assert states[-1]["file_id"] == "file-src"
    assert states[-1]["job_id"] == "job_1"
    assert any(state.get("status") == "running" for state in states)


@pytest.mark.asyncio
async def test_self_hosted_deduplicated_upload_skips_byte_upload(v1_server, pdf):
    v1_server.overrides[("POST", "/v1/uploads")] = httpx.Response(
        200, json={"id": "upload_1", "status": "completed", "file": {"id": "file-src"}}
    )

    markdown = await MinerUClient(_settings(mineru_tier=None, mineru_api_key=None)).parse(str(pdf))

    assert markdown == "# Self hosted result\n"
    assert [call[0] for call in v1_server.calls()][:2] == ["POST", "POST"]
    assert all("authorization" not in r.headers for r in v1_server.requests)
    assert "tier" not in json.loads(v1_server.requests[1].content)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "base_url",
    ["http://127.0.0.1:18000", "http://localhost:8000", "http://192.168.1.20:8000", "http://mineru.local"],
)
async def test_private_base_url_rejected_unless_explicitly_allowed(v1_server, pdf, base_url):
    v1_server.base = base_url
    with pytest.raises(ConfigurationError, match="SAG_MINERU_ALLOW_PRIVATE_BASE_URL"):
        await MinerUClient(
            _settings(mineru_base_url=base_url, mineru_allow_private_base_url=False)
        ).parse(str(pdf))
    assert v1_server.requests == []


@pytest.mark.asyncio
async def test_private_base_url_allowed_when_enabled(v1_server, pdf):
    v1_server.base = "http://192.168.1.20:8000"
    markdown = await MinerUClient(
        _settings(mineru_base_url="http://192.168.1.20:8000", mineru_allow_private_base_url=True)
    ).parse(str(pdf))
    assert markdown == "# Self hosted result\n"
    assert v1_server.requests[0].url.host == "192.168.1.20"


@pytest.mark.asyncio
async def test_public_base_url_works_without_private_switch(v1_server, pdf):
    v1_server.base = "http://8.8.8.8:8000"
    markdown = await MinerUClient(
        _settings(mineru_base_url="http://8.8.8.8:8000", mineru_allow_private_base_url=False)
    ).parse(str(pdf))
    assert markdown == "# Self hosted result\n"


@pytest.mark.asyncio
async def test_result_redirect_must_stay_same_origin(v1_server, pdf):
    v1_server.overrides[("GET", "/v1/files/file-zip/content")] = httpx.Response(
        302, headers={"location": "http://127.0.0.1:9999/leak.zip"}
    )

    with pytest.raises(UpstreamError, match="不同源"):
        await MinerUClient(_settings()).parse(str(pdf))

    assert all(r.url.port != 9999 for r in v1_server.requests)


@pytest.mark.asyncio
async def test_same_origin_result_redirect_is_followed(v1_server, pdf):
    zip_response = httpx.Response(200, content=_v1_zip("# Redirected"))
    v1_server.overrides[("GET", "/v1/files/file-zip/content")] = httpx.Response(
        302, headers={"location": "/v1/blobs/zip"}
    )
    v1_server.overrides[("GET", "/v1/blobs/zip")] = zip_response

    assert await MinerUClient(_settings()).parse(str(pdf)) == "# Redirected\n"
    assert v1_server.requests[-1].headers["authorization"] == f"Bearer {API_KEY}"


@pytest.mark.asyncio
async def test_cross_origin_upload_omits_key_and_keeps_ssrf_check(v1_server, pdf):
    v1_server.upload_url = "https://8.8.4.4/bucket/object?signature=x"

    assert await MinerUClient(_settings()).parse(str(pdf)) == "# Self hosted result\n"
    put = next(r for r in v1_server.requests if r.method == "PUT")
    assert put.url.host == "8.8.4.4"
    assert "authorization" not in put.headers

    v1_server.requests.clear()
    v1_server.upload_url = "http://10.0.0.5:9000/bucket/object"
    with pytest.raises(UpstreamError, match="内网"):
        await MinerUClient(_settings()).parse(str(pdf))
    assert all(r.method != "PUT" for r in v1_server.requests)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("job", "message"),
    [
        (
            _job(
                "job_1",
                "failed",
                "failed",
                error={"type": "engine_error", "code": "parse_failed", "message": "bad pdf", "param": None},
            ),
            "MinerU 解析失败：bad pdf",
        ),
        (_job("job_1", "canceled", "queued"), "已取消"),
        (
            _job(
                "job_1",
                "partial",
                "failed",
                error={"type": "engine_error", "code": "parse_failed", "message": "page 2", "param": None},
            ),
            "page 2",
        ),
        (_job("job_1", "completed", "completed"), "没有 zip"),
        ({**_job("job_1", "running", "running"), "status": "weird"}, "未知任务状态"),
    ],
)
async def test_terminal_failures_raise_upstream_error(v1_server, pdf, job, message):
    v1_server.polls = [job]
    with pytest.raises(UpstreamError, match=message):
        await MinerUClient(_settings()).parse(str(pdf))


@pytest.mark.asyncio
async def test_poll_timeout_keeps_job_for_resume(v1_server, pdf):
    v1_server.polls = [_job("job_1", "running", "running")]
    states: list[dict[str, Any]] = []

    with pytest.raises(ServiceUnavailableError, match="job_1"):
        await MinerUClient(_settings(mineru_poll_timeout=0.001)).parse(
            str(pdf), on_state=lambda state: _collect(states, state)
        )

    assert states[-1]["job_id"] == "job_1"


@pytest.mark.asyncio
async def test_request_timeout_and_auth_errors_are_mapped(v1_server, pdf):
    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    v1_server.overrides[("POST", "/v1/uploads")] = timeout
    with pytest.raises(ServiceUnavailableError, match="超时"):
        await MinerUClient(_settings()).parse(str(pdf))

    v1_server.overrides[("POST", "/v1/uploads")] = httpx.Response(
        401, json={"error": {"type": "authentication_error", "message": "bad key"}}
    )
    with pytest.raises(ConfigurationError, match="API Key"):
        await MinerUClient(_settings()).parse(str(pdf))


@pytest.mark.asyncio
async def test_resume_polls_saved_job_and_resubmits_after_service_restart(v1_server, pdf):
    v1_server.polls = [_completed_job()]
    markdown = await MinerUClient(_settings()).parse(
        str(pdf), state={"file_id": "file-src", "job_id": "job_1"}
    )
    assert markdown == "# Self hosted result\n"
    assert [r.url.path for r in v1_server.requests] == [
        "/v1/parse/jobs/job_1",
        "/v1/files/file-zip/content",
    ]

    v1_server.requests.clear()
    v1_server.polls = [_completed_job()]
    markdown = await MinerUClient(_settings()).parse(
        str(pdf), state={"file_id": "file-old", "job_id": "job_lost"}
    )
    assert markdown == "# Self hosted result\n"
    paths = [r.url.path for r in v1_server.requests]
    assert paths[0] == "/v1/parse/jobs/job_lost"
    assert paths[1:4] == ["/v1/uploads", "/v1/uploads/upload_1/content", "/v1/uploads/upload_1/complete"]


@pytest.mark.asyncio
async def test_pause_interrupts_polling(v1_server, pdf):
    async def pause() -> bool:
        return True

    states: list[dict[str, Any]] = []
    with pytest.raises(ParsePaused):
        await MinerUClient(_settings()).parse(
            str(pdf), on_state=lambda state: _collect(states, state), should_pause=pause
        )
    assert states[-1]["job_id"] == "job_1"
    assert all(r.url.path != "/v1/parse/jobs/job_1" for r in v1_server.requests)


@pytest.mark.asyncio
async def test_prepare_document_uses_self_hosted_and_falls_back_to_markitdown(
    v1_server, tmp_path, monkeypatch
):
    monkeypatch.setattr(service, "_markitdown_sync", lambda _path: "# Local fallback\n")
    ok_pdf = tmp_path / "ok.pdf"
    ok_pdf.write_bytes(b"%PDF-1.4 ok")
    prepared = await service.prepare_document(str(ok_pdf), _settings())
    assert prepared.provider == "mineru"
    assert prepared.path.endswith(".parsed.mineru-v1-flash-auto.md")

    failed_pdf = tmp_path / "failed.pdf"
    failed_pdf.write_bytes(b"%PDF-1.4 failed")
    prepared = await service.prepare_document(
        str(failed_pdf), _settings(mineru_allow_private_base_url=False)
    )
    assert prepared.provider == "markitdown"
    assert prepared.fallback_from == "mineru"
    assert "SAG_MINERU_ALLOW_PRIVATE_BASE_URL" in (prepared.fallback_error or "")


def test_self_hosted_settings_and_env_provider(monkeypatch):
    assert _settings(mineru_api_key=None).mineru_configured
    assert _settings(mineru_api_key=None).effective_document_parser == "mineru"
    assert not _settings(mineru_provider="302", mineru_api_key=None).mineru_configured
    assert Settings(_env_file=None).mineru_allow_private_base_url is False

    settings = _settings()
    settings_service.apply_overrides(settings, {"llm_model": "x"})
    assert settings.mineru_provider == "self_hosted"
    settings_service.apply_overrides(settings, {"mineru_base_url": "https://mineru.net"})
    assert settings.mineru_provider == "official"


def test_v1_zip_prefers_markdown_md():
    target = io.BytesIO()
    with zipfile.ZipFile(target, "w") as archive:
        archive.writestr("README.md", "# not the result\n" * 100)
        archive.writestr("markdown.md", "# V1 result")
    assert _markdown_from_zip(target.getvalue(), 1024 * 1024) == "# V1 result\n"


def test_invalid_self_hosted_base_url_is_configuration_error():
    with pytest.raises(ConfigurationError):
        MinerUClient(_settings(mineru_base_url="http://user:pw@127.0.0.1:8000"))
    with pytest.raises(ConfigurationError):
        MinerUClient(_settings(mineru_base_url="ftp://127.0.0.1"))


def test_blank_tier_env_means_server_default(monkeypatch):
    monkeypatch.setenv("SAG_MINERU_TIER", "")
    monkeypatch.setenv("SAG_MINERU_ALLOW_PRIVATE_BASE_URL", "false")
    settings = Settings(_env_file=None)
    assert settings.mineru_tier is None
    assert settings.mineru_allow_private_base_url is False
