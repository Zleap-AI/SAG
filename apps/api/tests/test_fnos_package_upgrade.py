"""The native installer completes legacy rebuilding before it starts SAG."""

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

API = Path(__file__).resolve().parents[1]
REPO = API.parents[1]
BACKUP_TOOL = REPO / "packages/fnos/native/sag/app/runtime/lifecycle.py"


def fixture(destination, version):
    with tarfile.open(API / f"tests/fixtures/fnos_{version}/native-{version}.tar.gz") as archive:
        archive.extractall(destination, filter="data")
    return destination / "users"


def hashes(root):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob("*") if p.is_file()}


def rows(database, table):
    with sqlite3.connect(database) as connection:
        return connection.execute(f'SELECT * FROM "{table}" ORDER BY id').fetchall()


def run_upgrade(data, *, operation="UPGRADE", locked=True):
    (data / "run").mkdir(exist_ok=True)
    env = {**os.environ, "TRIM_PKGVAR": str(data), "TRIM_APP_STATUS": operation,
           "PYTHONPATH": str(API), "UPGRADE_PYTHON": sys.executable,
           "UPGRADE_BACKUP_TOOL": str(BACKUP_TOOL)}
    lock = ("fd=os.open(os.environ['TRIM_PKGVAR']+'/run/lifecycle.lock',os.O_CREAT|os.O_RDWR,0o600);"
            "os.dup2(fd,9,inheritable=True);fcntl.flock(9,fcntl.LOCK_EX);") if locked else ""
    launcher = ("import os,sys,fcntl;" + lock
                + "os.execv(sys.executable,[sys.executable,'-m','sag_api.fnos.package_upgrade',"
                "'--root',os.environ['TRIM_PKGVAR'],'--backup-tool',os.environ['UPGRADE_BACKUP_TOOL']])")
    return subprocess.run(
        [sys.executable, "-c", launcher],
        env=env, capture_output=True, text=True, timeout=40,
    )


def test_installer_rebuilds_legacy_before_start_and_retains_models_history_files(tmp_path):
    users = fixture(tmp_path, "071")
    old_files = {name: value for name, value in hashes(users).items() if "/meta/" not in name}
    old_threads = {p.name: rows(p / "meta/sag.db", "threads") for p in users.iterdir()}
    old_settings = {p.name: rows(p / "meta/sag.db", "settings") for p in users.iterdir()}
    result = run_upgrade(tmp_path)
    assert result.returncode == 0, result.stderr
    for workspace in users.iterdir():
        with sqlite3.connect(workspace / "meta/sag.db") as connection:
            assert connection.execute("SELECT count(*) FROM documents").fetchone()[0] == 0
            assert connection.execute("SELECT count(*) FROM sources").fetchone()[0] == 0
            citations = json.loads(connection.execute("SELECT citations_json FROM messages").fetchone()[0])
            assert citations[0]["stale"] is True
            assert "stale" not in citations[1]
        assert rows(workspace / "meta/sag.db", "threads") == old_threads[workspace.name]
        assert all(row in rows(workspace / "meta/sag.db", "settings") for row in old_settings[workspace.name])
    assert {name: value for name, value in hashes(users).items() if "/meta/" not in name} == old_files
    archives = list((tmp_path / "backup").glob("*.tar.gz"))
    assert len(archives) == 1
    validation = subprocess.run([sys.executable, str(BACKUP_TOOL), "validate", "--archive", str(archives[0])],
                                capture_output=True, text=True)
    assert validation.returncode == 0, validation.stderr
    before_retry = hashes(users)
    assert run_upgrade(tmp_path).returncode == 0
    assert hashes(users) == before_retry
    assert len(list((tmp_path / "backup").glob("*.tar.gz"))) == 2


def test_compatible_upgrade_does_not_rebuild_or_rewrite_knowledge(tmp_path):
    users = fixture(tmp_path, "013")
    before = hashes(users)
    result = run_upgrade(tmp_path)
    assert result.returncode == 0, result.stderr
    assert hashes(users) == before
    assert len(list((tmp_path / "backup").glob("*.tar.gz"))) == 1


def test_mixed_users_only_rebuilds_the_legacy_workspace(tmp_path):
    users = fixture(tmp_path, "071")
    current = fixture(tmp_path / "current", "013")
    bob = next(p for p in users.iterdir() if p.name != "1000")
    shutil.rmtree(bob)
    shutil.copytree(next(p for p in current.iterdir() if p.name != "1000"), bob)
    before = hashes(bob)
    result = run_upgrade(tmp_path)
    assert result.returncode == 0, result.stderr
    assert hashes(bob) == before
    assert rows(users / "1000/meta/sag.db", "documents") == []


def test_unknown_user_blocks_all_rebuilds_before_any_metadata_change(tmp_path):
    users = fixture(tmp_path, "071")
    bob = next(p for p in users.iterdir() if p.name != "1000")
    (bob / "meta/sag.db").write_bytes(b"not SQLite")
    before = hashes(users)
    result = run_upgrade(tmp_path)
    assert result.returncode != 0
    assert hashes(users) == before
    assert not list((tmp_path / "backup").glob("*.tar.gz"))


def test_rebuild_failure_restores_all_tenant_metadata(tmp_path):
    users = fixture(tmp_path, "071")
    bob = next(p for p in users.iterdir() if p.name != "1000")
    with sqlite3.connect(bob / "meta/sag.db") as connection:
        connection.execute("CREATE TRIGGER reject_rebuild BEFORE DELETE ON documents "
                           "BEGIN SELECT RAISE(ABORT, 'fixture refuses deletion'); END")
    before = hashes(users)
    result = run_upgrade(tmp_path)
    assert result.returncode != 0
    assert hashes(users) == before
    assert len(list((tmp_path / "backup").glob("*.tar.gz"))) == 1


def test_normal_start_cannot_authorize_a_package_rebuild(tmp_path):
    users = fixture(tmp_path, "071")
    before = hashes(users)
    result = run_upgrade(tmp_path, operation="START")
    assert result.returncode != 0
    assert hashes(users) == before
    result = run_upgrade(tmp_path, locked=False)
    assert result.returncode != 0
    assert hashes(users) == before


@pytest.mark.parametrize("failed_flush", [1, 2])
def test_backup_flush_failure_prevents_upgrade_completion(tmp_path, monkeypatch, failed_flush):
    from sag_api.fnos.package_upgrade import upgrade_users

    users = fixture(tmp_path, "013")
    before = hashes(users)
    monkeypatch.setenv("TRIM_PKGVAR", str(tmp_path))
    real_flush = os.fsync
    calls = 0

    def fail_flush(fd):
        nonlocal calls
        calls += 1
        if calls == failed_flush:
            raise OSError("fixture disk flush failed")
        return real_flush(fd)

    monkeypatch.setattr(os, "fsync", fail_flush)
    with pytest.raises(OSError, match="disk flush failed"):
        upgrade_users(tmp_path, BACKUP_TOOL)
    assert hashes(users) == before
    assert not list((tmp_path / "backup").glob("*.tmp"))
