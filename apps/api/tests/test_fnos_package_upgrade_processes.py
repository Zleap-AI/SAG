"""Installer interruption must stop all writers and release the cold-backup boundary."""
import fcntl
import hashlib
import json
import os
import signal
import sqlite3
import subprocess
import sys
import tarfile
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
FIXTURE = REPO / 'apps/api/tests/fixtures/fnos_071/native-071.tar.gz'
BACKUP_TOOL = REPO / 'packages/fnos/native/sag/app/runtime/lifecycle.py'


def hashes(root):
    return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in root.rglob('*') if path.is_file()}


def process_state(pid):
    try:
        return Path(f'/proc/{pid}/stat').read_text().split()[2]
    except FileNotFoundError:
        return 'gone'


def alive(pid):
    return process_state(pid) not in {'gone', 'Z'}


def children(pid):
    result = []
    for path in Path('/proc').glob('[0-9]*/stat'):
        try:
            fields = path.read_text().split()
            if int(fields[3]) == pid:
                result.append(int(fields[0]))
        except (FileNotFoundError, ProcessLookupError):
            pass
    return result


@pytest.mark.skipif(sys.platform != "linux", reason="requires Linux parent-death signals and /proc")
@pytest.mark.parametrize("mode", ["term", "parent-death", "backup-parent-death"])
def test_installer_interruption_reaps_writers_and_restores_metadata(tmp_path, mode):
    root = tmp_path
    data = root / 'data'
    data.mkdir()
    (data / 'run').mkdir()
    with tarfile.open(FIXTURE) as archive:
        archive.extractall(data, filter='data')
    database = data / 'users/1000/meta/sag.db'
    if mode == 'backup-parent-death':
        (data / 'users/1000/uploads/slow-backup.bin').write_bytes(os.urandom(64 * 1024**2))
    else:
        with sqlite3.connect(database) as connection:
            connection.execute('CREATE TRIGGER slow_rebuild BEFORE DELETE ON documents BEGIN '
                               'SELECT count(*) FROM (WITH RECURSIVE c(x) AS '
                               '(SELECT 1 UNION ALL SELECT x+1 FROM c WHERE x<50000000) '
                               'SELECT x FROM c); END')
    before = hashes(data / 'users')
    pidfile = root / 'coordinator.pid'
    launcher = (
        "import fcntl,os,subprocess,sys;from pathlib import Path;"
        "fd=os.open(os.environ['TRIM_PKGVAR']+'/run/lifecycle.lock',os.O_CREAT|os.O_RDWR,0o600);"
        "os.dup2(fd,9,inheritable=True);fcntl.flock(9,fcntl.LOCK_EX);"
        "p=subprocess.Popen([sys.executable,'-m','sag_api.fnos.package_upgrade','--root',"
        "os.environ['TRIM_PKGVAR'],'--backup-tool',os.environ['BACKUP_TOOL']],pass_fds=(9,));"
        "Path(os.environ['PIDFILE']).write_text(str(p.pid));sys.exit(p.wait())"
    )
    environment = dict(os.environ, TRIM_PKGVAR=str(data), TRIM_APP_STATUS='UPGRADE',
                       PIDFILE=str(pidfile), BACKUP_TOOL=str(BACKUP_TOOL),
                       PYTHONPATH=str(REPO / 'apps/api'))
    coordinator = None
    workers = []
    with (root / 'console.log').open('w') as console:
        installer = subprocess.Popen([sys.executable, '-c', launcher], env=environment,
                                     stdout=console, stderr=console)
        try:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                raw_pid = pidfile.read_text().strip() if pidfile.exists() else ""
                if raw_pid.isdecimal():
                    coordinator = int(raw_pid)
                    break
                time.sleep(.01)
            assert coordinator is not None, "coordinator PID was not published"
            busy_since = None
            observed = False
            while time.monotonic() < deadline:
                if mode == 'backup-parent-death':
                    observed = any(path.stat().st_size > 1024**2
                                   for path in (data / 'backup').glob('*.tmp'))
                else:
                    busy = False
                    connection = sqlite3.connect(database, timeout=0)
                    try:
                        connection.execute('BEGIN IMMEDIATE')
                        connection.rollback()
                    except sqlite3.OperationalError as error:
                        busy = 'locked' in str(error)
                    finally:
                        connection.close()
                    if busy and list((data / 'backup').glob('sag-upgrade-meta-*')):
                        busy_since = busy_since or time.monotonic()
                        observed = time.monotonic() - busy_since > .25
                    else:
                        busy_since = None
                if observed:
                    break
                assert installer.poll() is None, (root / 'console.log').read_text()
                time.sleep(.01)
            assert observed, 'target backup/transaction stage was not observed'
            workers = children(coordinator)
            with (data / 'run/lifecycle.lock').open('rb') as lock:
                try:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    pass
                else:
                    raise AssertionError('exclusive lifecycle lock was not held')
            if mode == 'term':
                os.kill(coordinator, signal.SIGTERM)
            else:
                os.kill(installer.pid, signal.SIGKILL)
            deadline = time.monotonic() + 12
            while alive(coordinator) and time.monotonic() < deadline:
                time.sleep(.02)
            summary = {'mode': mode, 'coordinator_state': process_state(coordinator),
                       'worker_states': [process_state(pid) for pid in workers]}
            print(json.dumps(summary), flush=True)
            assert not alive(coordinator), 'orphan metadata coordinator remains alive'
            assert not any(alive(pid) for pid in workers), 'orphan tenant writer remains alive'
            if installer.poll() is None:
                installer.wait(timeout=3)
            with (data / 'run/lifecycle.lock').open('rb') as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            after = hashes(data / 'users')
            assert after == before, {
                'changed': [key for key in set(before) | set(after) if before.get(key) != after.get(key)],
                'console': (root / 'console.log').read_text(),
            }
            assert not list((data / 'backup').glob('*.tmp'))
            assert not list((data / 'backup').glob('sag-upgrade-meta-*'))
            print(json.dumps(dict(mode=mode, outcome='PASS', all_tenant_hashes_restored=True,
                                  lifecycle_lock_released=True, partial_files_removed=True)), flush=True)
        finally:
            for pid in workers + ([coordinator] if coordinator else []):
                if alive(pid):
                    os.kill(pid, signal.SIGKILL)
            if installer.poll() is None:
                installer.kill()
            installer.wait(timeout=3)
