from __future__ import annotations

import hashlib
import importlib.util
from io import BytesIO

import pytest
from starlette.datastructures import UploadFile


def test_desktop_main_prepares_frozen_multiprocessing_before_uvicorn(monkeypatch):
    """A frozen child must dispatch to multiprocessing instead of starting a second API."""
    from sag_api import desktop

    calls: list[str] = []
    monkeypatch.setattr(
        desktop.multiprocessing,
        "freeze_support",
        lambda: calls.append("freeze_support"),
    )
    monkeypatch.setattr(
        desktop.uvicorn,
        "run",
        lambda *_args, **_kwargs: calls.append("uvicorn"),
    )

    desktop.main()

    assert calls == ["freeze_support", "uvicorn"]


def test_worker_pipe_eof_reports_non_empty_exit_diagnostics():
    """A crashed worker must not degrade into an empty EOFError in the UI."""
    from sag_api.octx.runner import _worker_exit_error

    error = _worker_exit_error("build", exitcode=7, memory_mb=2048)

    assert error.error_type == "OctxWorkerExited"
    assert error.exitcode == 7
    assert str(error) == "OCTX build worker exited unexpectedly (exit_code=7)"


@pytest.mark.parametrize("exitcode", (-9, -6))
def test_worker_oom_like_exit_is_classified_as_resource_limit(exitcode):
    """A SIGKILL/SIGABRT child exit must fail only the task as a resource limit."""
    from sag_api.octx.runner import OctxWorkerResourceLimitError, _worker_exit_error

    error = _worker_exit_error("build", exitcode=exitcode, memory_mb=2048)

    assert isinstance(error, OctxWorkerResourceLimitError)
    assert error.error_type == "OctxWorkerMemoryLimit"
    assert error.exitcode == exitcode
    assert "2048 MB" in str(error)


def test_worker_reported_memory_error_is_classified_as_resource_limit():
    """A caught Python MemoryError must use the same bounded-task failure path."""
    from sag_api.octx.runner import OctxWorkerResourceLimitError, _worker_reported_error

    error = _worker_reported_error(
        "build",
        {"error_type": "MemoryError", "message": "", "report": None},
        memory_mb=2048,
    )

    assert isinstance(error, OctxWorkerResourceLimitError)
    assert error.error_type == "OctxWorkerMemoryLimit"
    assert "2048 MB" in str(error)


@pytest.mark.asyncio
async def test_runner_maps_worker_memory_limit_to_non_retryable_resource_error(monkeypatch):
    """Worker OOM must fail only the task with a stable user-facing error code."""
    from sag_api.core.config import Settings
    from sag_api.core.errors import ApiError
    from sag_api.octx import runner as runner_module
    from sag_api.octx.runner import OctxRunner, OctxWorkerResourceLimitError

    async def raise_memory_limit(*_args, **_kwargs):
        raise OctxWorkerResourceLimitError("bounded worker OOM", exitcode=-9)

    monkeypatch.setattr(runner_module.asyncio, "to_thread", raise_memory_limit)

    with pytest.raises(ApiError) as caught:
        await OctxRunner(Settings(_env_file=None))._execute("build", {})

    assert caught.value.to_envelope()["error"] == {
        "code": "octx_resource_limit",
        "message": "bounded worker OOM",
        "layer": "api",
        "stage": "octx_publish",
        "retryable": False,
    }


def test_octx_runtime_modules_are_present():
    """Missing an adapter module would push unsafe file handling into API handlers."""
    for name in ("limits", "storage", "errors", "runner"):
        assert importlib.util.find_spec(f"sag_api.octx.{name}") is not None


def test_archive_limits_are_the_single_byte_conversion_boundary():
    """Using MB values as bytes would silently disable or over-tighten archive limits."""
    from sag_api.core.config import Settings
    from sag_api.octx.limits import build_archive_limits

    limits = build_archive_limits(
        Settings(
            _env_file=None,
            octx_max_upload_mb=3,
            octx_max_entries=17,
            octx_max_file_mb=5,
            octx_max_uncompressed_mb=11,
            octx_max_compression_ratio=42,
            octx_max_jsonl_line_mb=2,
            octx_max_jsonl_records=23,
            octx_max_issues=7,
        )
    )

    assert limits.max_entries == 17
    assert limits.max_file_size == 5 * 1024 * 1024
    assert limits.max_total_uncompressed == 11 * 1024 * 1024
    assert limits.max_compression_ratio == 42
    assert limits.max_jsonl_line_size == 2 * 1024 * 1024
    assert limits.max_jsonl_records == 23
    assert limits.max_issues == 7


@pytest.mark.asyncio
async def test_stream_upload_is_bounded_read_only_and_content_addressed(tmp_path):
    """An unbounded read or mutable post-preflight file would bypass validation guarantees."""
    from sag_api.octx.storage import OctxStorage

    storage = OctxStorage(tmp_path / "octx", max_upload_bytes=8)
    upload = UploadFile(filename="source.octx", file=BytesIO(b"12345678"))

    stored = await storage.stream_upload(upload, "transfer-a", chunk_size=3)

    assert stored.size_bytes == 8
    assert stored.sha256 == "ef797c8118f02dfb649607dd5d3f8c7623048c9c063d532cc95c5ed7a898a64f"
    assert stored.path.read_bytes() == b"12345678"
    assert stored.path.stat().st_mode & 0o777 == 0o400
    assert stored.key == "staging/transfer-a/input.octx"

    too_large = UploadFile(filename="large.octx", file=BytesIO(b"123456789"))
    with pytest.raises(ValueError, match="upload limit"):
        await storage.stream_upload(too_large, "transfer-b", chunk_size=3)
    assert not storage.staging_dir("transfer-b").exists()


def test_storage_rejects_escape_and_never_overwrites_release(tmp_path):
    """A crafted artifact key or duplicate publish must not escape or mutate immutable releases."""
    from sag_api.octx.storage import OctxStorage

    storage = OctxStorage(tmp_path / "octx", max_upload_bytes=1024)
    with pytest.raises(ValueError, match="artifact key"):
        storage.resolve_key("../outside.octx")
    with pytest.raises(ValueError, match="component"):
        storage.release_key("../asset", "1.0.0", "sha256:" + "a" * 64)

    first = tmp_path / "first.octx"
    first.write_bytes(b"first")
    key = storage.publish_release(first, "0191f6a0-0000-7000-8000-000000000001", "1.0.0", "sha256:" + "a" * 64)
    assert storage.resolve_key(key).read_bytes() == b"first"

    second = tmp_path / "second.octx"
    second.write_bytes(b"second")
    with pytest.raises(FileExistsError):
        storage.publish_release(
            second,
            "0191f6a0-0000-7000-8000-000000000001",
            "1.0.0",
            "sha256:" + "a" * 64,
        )
    assert storage.resolve_key(key).read_bytes() == b"first"


def test_octx_errors_map_to_stable_sag_envelopes():
    """Treating resource limits as retryable generic failures would create retry storms."""
    from octx.errors import OctxFormatError, OctxResourceLimitError

    from sag_api.core.error_taxonomy import ErrorStage
    from sag_api.octx.errors import map_octx_error

    resource = map_octx_error(OctxResourceLimitError("too large"), ErrorStage.OCTX_VALIDATE)
    assert resource.to_envelope()["error"] == {
        "code": "octx_resource_limit",
        "message": "too large",
        "layer": "api",
        "stage": "octx_validate",
        "retryable": False,
    }

    malformed = map_octx_error(OctxFormatError("bad manifest"), ErrorStage.OCTX_VALIDATE)
    assert malformed.code == "octx_invalid_package"
    assert malformed.status_code == 422


@pytest.mark.asyncio
async def test_runner_validates_real_octx_in_disposable_process(tmp_path):
    """Running validation in-process would expose the API worker to archive memory failures."""
    from octx import create_octx

    from sag_api.core.config import Settings
    from sag_api.octx.runner import OctxRunner
    from sag_api.octx.storage import FileSignature, StoredUpload

    source = tmp_path / "source"
    source.mkdir()
    (source / "index.md").write_text(
        '---\nokf_version: "1.0"\n---\n# Index\n\n- [Sample](sample.md)\n',
        encoding="utf-8",
    )
    (source / "sample.md").write_text("# Sample\n\nA bounded OCTX package.\n", encoding="utf-8")
    package = tmp_path / "sample.octx"
    create_octx(tmp_path / "workspace", source=source, output=package, name="Sample")
    payload = package.read_bytes()
    upload = StoredUpload(
        path=package,
        key="staging/test/input.octx",
        sha256=hashlib.sha256(payload).hexdigest(),
        size_bytes=len(payload),
        signature=FileSignature.from_path(package),
    )

    validated = await OctxRunner(Settings(_env_file=None, octx_worker_timeout_seconds=30)).validate_package(upload)

    assert validated.report["valid"] is True
    assert validated.report["fully_validated"] is True
    assert validated.manifest["asset"]["name"] == "Sample"
    assert validated.record_counts["documents"] == 1


def test_import_policy_allows_only_fully_checked_invalid_vector_layer():
    from sag_api.octx.runner import _report_is_importable

    base = {
        "format": {"valid": True, "fully_validated": True},
        "capabilities": {
            "sag-structured": {"valid": True, "fully_validated": True},
            "vectors": {"valid": False, "fully_validated": True},
        },
    }

    assert _report_is_importable(base) is True
    assert (
        _report_is_importable(
            {**base, "capabilities": {**base["capabilities"], "vectors": {"valid": None, "fully_validated": False}}}
        )
        is False
    )
    assert (
        _report_is_importable(
            {
                **base,
                "capabilities": {
                    **base["capabilities"],
                    "sag-structured": {"valid": False, "fully_validated": True},
                },
            }
        )
        is False
    )


def test_worker_reads_manifest_when_only_vector_layer_is_invalid(monkeypatch, tmp_path):
    """Invalid reusable vectors must not hide valid structured package metadata."""
    import sys
    from contextlib import contextmanager
    from types import SimpleNamespace

    from sag_api.octx._worker import _validate

    report_payload = {
        "valid": False,
        "fully_validated": True,
        "format": {"valid": True, "fully_validated": True},
        "capabilities": {
            "sag-structured": {"valid": True, "fully_validated": True},
            "vectors": {"valid": False, "fully_validated": True},
        },
    }
    report = SimpleNamespace(
        valid=False,
        fully_validated=True,
        to_dict=lambda: report_payload,
    )

    class Package:
        manifest = {"asset": {"id": "asset-1"}, "capabilities": {"sag-structured": {"version": "0.1"}}}
        available_paths = {"data/documents.jsonl", "data/chunks.jsonl"}

        @staticmethod
        def iter_documents():
            return iter(({"id": "doc-1"},))

        @staticmethod
        def iter_chunks():
            return iter(({"id": "chunk-1"},))

        @staticmethod
        def iter_events():
            return iter(())

        @staticmethod
        def iter_entities():
            return iter(())

        @staticmethod
        def iter_chunk_events():
            return iter(())

        @staticmethod
        def iter_event_entities():
            return iter(())

    @contextmanager
    def open_octx(*_args, **_kwargs):
        yield Package()

    monkeypatch.setitem(
        sys.modules,
        "octx",
        SimpleNamespace(
            ArchiveLimits=lambda **values: SimpleNamespace(**values),
            open_octx=open_octx,
            validate_octx=lambda *_args, **_kwargs: report,
        ),
    )

    result = _validate(
        {
            "path": str(tmp_path / "package.octx"),
            "limits": {"max_issues": 10},
        }
    )

    assert result["manifest"]["asset"]["id"] == "asset-1"
    assert result["record_counts"] == {
        "documents": 1,
        "chunks": 1,
        "events": 0,
        "entities": 0,
        "chunk_events": 0,
        "event_entities": 0,
    }


def test_real_structured_package_allows_chunks_without_events(tmp_path):
    """Requiring an Event for every Chunk would reject ordinary knowledge-only sources."""
    from octx import create_octx, validate_octx

    document_id = "018f5f7e-89ab-7def-8123-0123456789d0"
    chunk_id = "018f5f7e-89ab-7def-8123-0123456789d1"
    workspace = tmp_path / "workspace"
    knowledge = workspace / "knowledge"
    documents = knowledge / "documents"
    data = workspace / "data"
    relations = workspace / "relations"
    documents.mkdir(parents=True)
    data.mkdir()
    relations.mkdir()
    (knowledge / "index.md").write_text(
        '---\nokf_version: "1.0"\n---\n# Index\n\n- [Sample](documents/sample.md)\n',
        encoding="utf-8",
    )
    (documents / "sample.md").write_text(
        f'---\noctx:\n  document_id: "{document_id}"\n---\n# Sample\n\nKnowledge only.\n',
        encoding="utf-8",
    )
    (data / "chunks.jsonl").write_text(
        '{"id":"' + chunk_id + '","document_id":"' + document_id + '","ordinal":0,"text":"Knowledge only."}\n',
        encoding="utf-8",
    )
    for path in (
        data / "events.jsonl",
        data / "entities.jsonl",
        relations / "chunk-events.jsonl",
        relations / "event-entities.jsonl",
    ):
        path.write_text("", encoding="utf-8")

    package = tmp_path / "zero-events.octx"
    create_octx(
        workspace,
        output=package,
        name="Zero Events",
        capabilities={"sag-structured": "0.1"},
    )
    report = validate_octx(package)

    assert report.valid is True
    assert report.fully_validated is True


def test_real_structured_package_rejects_event_without_entity(tmp_path):
    """Accepting an Event without an Entity would publish a graph OCTX 0.1.3 cannot consume."""
    from octx import create_octx
    from octx.errors import OctxValidationError

    document_id = "018f5f7e-89ab-7def-8123-0123456789e0"
    chunk_id = "018f5f7e-89ab-7def-8123-0123456789e1"
    event_id = "018f5f7e-89ab-7def-8123-0123456789e2"
    workspace = tmp_path / "workspace"
    knowledge = workspace / "knowledge"
    documents = knowledge / "documents"
    data = workspace / "data"
    relations = workspace / "relations"
    documents.mkdir(parents=True)
    data.mkdir()
    relations.mkdir()
    (knowledge / "index.md").write_text(
        '---\nokf_version: "1.0"\n---\n# Index\n\n- [Sample](documents/sample.md)\n',
        encoding="utf-8",
    )
    (documents / "sample.md").write_text(
        f'---\noctx:\n  document_id: "{document_id}"\n---\n# Sample\n\nBody.\n',
        encoding="utf-8",
    )
    (data / "chunks.jsonl").write_text(
        f'{{"id":"{chunk_id}","document_id":"{document_id}","ordinal":0,"text":"Body."}}\n',
        encoding="utf-8",
    )
    (data / "events.jsonl").write_text(
        f'{{"id":"{event_id}","title":"Published","content":"Body."}}\n',
        encoding="utf-8",
    )
    (data / "entities.jsonl").write_text("", encoding="utf-8")
    (relations / "chunk-events.jsonl").write_text(
        f'{{"chunk_id":"{chunk_id}","event_id":"{event_id}"}}\n',
        encoding="utf-8",
    )
    (relations / "event-entities.jsonl").write_text("", encoding="utf-8")

    with pytest.raises(OctxValidationError) as caught:
        create_octx(
            workspace,
            output=tmp_path / "missing-entity.octx",
            name="Missing Entity",
            capabilities={"sag-structured": "0.1"},
        )

    assert "OCTX_SAG_EVENT_IN_ENTITY_RELATION" in caught.value.report.issue_codes
