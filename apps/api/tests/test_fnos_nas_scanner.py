from __future__ import annotations

import asyncio
import os
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from sag_api.db.models import Document, Source
from sag_api.enums import ConnectorKind, DocumentStatus, SourceType
from sag_api.fnos.identity import GatewayIdentity
from sag_api.fnos.nas_registry import FnOSNasScanRegistry
from sag_api.fnos.open_api import UserACL
from sag_api.services.fnos_nas_access import ResolvedNasRoot
from sag_api.services.fnos_nas_scanner import SCAN_LIMITS, FnOSNasScanner, nas_origin_key


class FakeACLHost:
    def __init__(self) -> None:
        self.unreadable: set[str] = set()
        self.batches: list[list[str]] = []

    async def check_user_acl(self, uid: int, paths: list[str]) -> list[UserACL]:
        assert uid == 1000
        self.batches.append(paths)
        return [
            UserACL(
                path=path,
                readable=path not in self.unreadable,
                writable=False,
                deletable=False,
            )
            for path in paths
        ]


@pytest.fixture
def secret_file(tmp_path: Path) -> Path:
    path = tmp_path / "identity.key"
    path.write_text("a" * 64, encoding="ascii")
    path.chmod(0o600)
    return path


@pytest.fixture
async def scanner_db() -> tuple[AsyncSession, Source]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Source.__table__.create)
        await connection.run_sync(Document.__table__.create)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session:
        source = Source(
            name="Private",
            source_type=SourceType.DOCUMENT,
            connector_kind=ConnectorKind.FILE_UPLOAD,
            sag_source_config_id="config-id",
        )
        session.add(source)
        await session.commit()
        yield session, source
    await engine.dispose()


def _settings() -> SimpleNamespace:
    return SimpleNamespace(allowed_upload_exts={".pdf", ".md", ".txt"}, max_upload_mb=1)


def _root(path: Path, source: str = "host_api") -> ResolvedNasRoot:
    return ResolvedNasRoot(
        path=path,
        authorized_path=str(path),
        display_path="Documents",
        source=source,
        folder_id="opaque-folder",
    )


def _scanner(
    host: FakeACLHost,
    secret_file: Path,
    *,
    limits: dict[str, int | float] | None = None,
    monotonic=None,
) -> FnOSNasScanner:
    return FnOSNasScanner(
        host,
        FnOSNasScanRegistry(secret_file=secret_file),
        settings_obj=_settings(),
        limits=limits,
        monotonic=monotonic,
    )


@pytest.mark.asyncio
async def test_scanner_walks_deterministically_filters_and_classifies(
    tmp_path: Path,
    scanner_db: tuple[AsyncSession, Source],
    secret_file: Path,
) -> None:
    session, source = scanner_db
    root = tmp_path / "Documents"
    (root / "nested").mkdir(parents=True)
    (root / "a.pdf").write_bytes(b"new")
    (root / "nested" / "B.md").write_text("changed", encoding="utf-8")
    (root / "z.txt").write_text("same", encoding="utf-8")
    (root / ".hidden.pdf").write_bytes(b"hidden")
    (root / "unsupported.exe").write_bytes(b"exe")
    with (root / "large.pdf").open("wb") as handle:
        handle.truncate(1024 * 1024 + 1)
    (root / "link.pdf").symlink_to(root / "a.pdf")
    (root / "linked-dir").symlink_to(root / "nested", target_is_directory=True)
    if hasattr(os, "mkfifo"):
        os.mkfifo(root / "pipe.pdf")

    nested = (root / "nested" / "B.md").resolve()
    existing = (root / "z.txt").resolve()
    session.add_all(
        [
            Document(
                source_id=source.id,
                filename="B.md",
                content_type="text/markdown",
                size_bytes=1,
                storage_path="/private/B.md",
                status=DocumentStatus.READY,
                origin_kind="fnos_shared",
                origin_key=nas_origin_key(root.resolve(), nested),
                origin_size_bytes=1,
                origin_mtime_ns=1,
            ),
            Document(
                source_id=source.id,
                filename="z.txt",
                content_type="text/plain",
                size_bytes=4,
                storage_path="/private/z.txt",
                status=DocumentStatus.READY,
                origin_kind="fnos_shared",
                origin_key=nas_origin_key(root.resolve(), existing),
                origin_size_bytes=existing.stat().st_size,
                origin_mtime_ns=existing.stat().st_mtime_ns,
            ),
        ]
    )
    await session.commit()
    host = FakeACLHost()
    scanner = _scanner(host, secret_file)

    result = await scanner.scan(
        session,
        identity=GatewayIdentity(1000, "Alice", True),
        source=source,
        root=_root(root),
        recursive=True,
    )

    assert [item.display_path for item in result.files] == [
        "a.pdf",
        "large.pdf",
        "nested/B.md",
        "unsupported.exe",
        "z.txt",
    ]
    assert {item.display_path: item.state for item in result.files} == {
        "a.pdf": "new",
        "large.pdf": "too_large",
        "nested/B.md": "changed",
        "unsupported.exe": "unsupported",
        "z.txt": "imported",
    }
    selectable = [item for item in result.files if item.selection_token]
    assert [item.selected_by_default for item in selectable] == [True, True, False]
    assert all("/vol" not in (item.selection_token or "") for item in result.files)
    assert result.summary.new == 1
    assert result.summary.changed == 1
    assert result.summary.imported == 1
    assert result.summary.unsupported == 1
    assert result.summary.too_large == 1
    assert result.truncated is False
    assert all(len(batch) <= SCAN_LIMITS["acl_batch_size"] for batch in host.batches)


@pytest.mark.asyncio
async def test_scanner_non_recursive_and_legacy_mode_skip_host_acl(
    tmp_path: Path, scanner_db: tuple[AsyncSession, Source], secret_file: Path
) -> None:
    session, source = scanner_db
    root = tmp_path / "Documents"
    (root / "nested").mkdir(parents=True)
    (root / "a.pdf").write_bytes(b"a")
    (root / "nested" / "b.md").write_bytes(b"b")
    host = FakeACLHost()

    result = await _scanner(host, secret_file).scan(
        session,
        identity=GatewayIdentity(1000, "Alice", True),
        source=source,
        root=_root(root, "legacy_manual"),
        recursive=False,
    )

    assert [item.display_path for item in result.files] == ["a.pdf"]
    assert host.batches == []


@pytest.mark.asyncio
async def test_scanner_batches_acl_and_marks_unreadable(
    tmp_path: Path, scanner_db: tuple[AsyncSession, Source], secret_file: Path
) -> None:
    session, source = scanner_db
    root = tmp_path / "Documents"
    root.mkdir()
    for index in range(205):
        (root / f"{index:03}.txt").write_bytes(b"x")
    host = FakeACLHost()
    host.unreadable.add(str((root / "003.txt").resolve()))

    result = await _scanner(host, secret_file).scan(
        session,
        identity=GatewayIdentity(1000, "Alice", True),
        source=source,
        root=_root(root),
        recursive=True,
    )

    assert [len(batch) for batch in host.batches] == [100, 100, 5]
    assert result.summary.unreadable == 1
    unreadable = next(item for item in result.files if item.display_path == "003.txt")
    assert unreadable.state == "unreadable"
    assert unreadable.selection_token is None


@pytest.mark.asyncio
async def test_scanner_returns_partial_results_at_limits(
    tmp_path: Path, scanner_db: tuple[AsyncSession, Source], secret_file: Path
) -> None:
    session, source = scanner_db
    root = tmp_path / "Documents"
    root.mkdir()
    for name in ("a.pdf", "b.pdf", "c.pdf"):
        (root / name).write_bytes(b"x")
    host = FakeACLHost()
    limits = {**SCAN_LIMITS, "returned_files": 2}

    result = await _scanner(host, secret_file, limits=limits).scan(
        session,
        identity=GatewayIdentity(1000, "Alice", True),
        source=source,
        root=_root(root),
        recursive=True,
    )

    assert [item.display_path for item in result.files] == ["a.pdf", "b.pdf"]
    assert result.truncated is True
    assert result.truncated_reason == "file_limit"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("limit_name", "limit_value", "expected_reason"),
    [
        ("visited_entries", 1, "entry_limit"),
        ("wall_seconds", 0, "time_limit"),
    ],
)
async def test_scanner_stops_at_entry_and_time_limits(
    limit_name: str,
    limit_value: int,
    expected_reason: str,
    tmp_path: Path,
    scanner_db: tuple[AsyncSession, Source],
    secret_file: Path,
) -> None:
    session, source = scanner_db
    root = tmp_path / "Documents"
    root.mkdir()
    (root / "a.pdf").write_bytes(b"a")
    (root / "b.pdf").write_bytes(b"b")
    limits = {**SCAN_LIMITS, limit_name: limit_value}

    result = await _scanner(FakeACLHost(), secret_file, limits=limits).scan(
        session,
        identity=GatewayIdentity(1000, "Alice", True),
        source=source,
        root=_root(root),
        recursive=True,
    )

    assert result.truncated is True
    assert result.truncated_reason == expected_reason


@pytest.mark.asyncio
async def test_scanner_stops_at_depth_limit(
    tmp_path: Path, scanner_db: tuple[AsyncSession, Source], secret_file: Path
) -> None:
    session, source = scanner_db
    root = tmp_path / "Documents"
    (root / "one" / "two").mkdir(parents=True)
    (root / "one" / "two" / "deep.pdf").write_bytes(b"x")
    limits = {**SCAN_LIMITS, "max_depth": 1}

    result = await _scanner(FakeACLHost(), secret_file, limits=limits).scan(
        session,
        identity=GatewayIdentity(1000, "Alice", True),
        source=source,
        root=_root(root),
        recursive=True,
    )

    assert result.files == []
    assert result.truncated_reason == "depth_limit"


@pytest.mark.asyncio
async def test_scanner_cancellation_signals_background_walk(
    tmp_path: Path, scanner_db: tuple[AsyncSession, Source], secret_file: Path
) -> None:
    session, source = scanner_db
    root = tmp_path / "Documents"
    root.mkdir()
    scanner = _scanner(FakeACLHost(), secret_file)
    started = threading.Event()
    stopped = threading.Event()

    def blocking_walk(_root_path: Path, _recursive: bool, cancel: threading.Event):
        started.set()
        cancel.wait(2)
        stopped.set()
        return None

    scanner._walk = blocking_walk  # type: ignore[method-assign]
    task = asyncio.create_task(
        scanner.scan(
            session,
            identity=GatewayIdentity(1000, "Alice", True),
            source=source,
            root=_root(root),
            recursive=True,
        )
    )
    assert await asyncio.to_thread(started.wait, 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await asyncio.to_thread(stopped.wait, 1)


@pytest.mark.asyncio
async def test_scanner_rejects_symlink_or_non_directory_root(
    tmp_path: Path, scanner_db: tuple[AsyncSession, Source], secret_file: Path
) -> None:
    session, source = scanner_db
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    file = tmp_path / "file.pdf"
    file.write_bytes(b"x")
    scanner = _scanner(FakeACLHost(), secret_file)

    for unsafe in (link, file, tmp_path / "missing"):
        with pytest.raises(Exception, match="扫描目录"):
            await scanner.scan(
                session,
                identity=GatewayIdentity(1000, "Alice", True),
                source=source,
                root=_root(unsafe),
                recursive=True,
            )
