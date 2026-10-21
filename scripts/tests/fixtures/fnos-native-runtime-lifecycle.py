"""Real-process tests of the package's public lifecycle callbacks only."""
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest

ROOT = Path(sys.argv.pop(1))
CASE = sys.argv.pop(1)
CMD = ROOT / "packages/fnos/native/sag/cmd"


def wait_until(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("fixture did not become ready")


class Lifecycle(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="sag-lifecycle-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.app = self.root / "app"
        self.app.mkdir()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.env = dict(os.environ, TRIM_APPDEST=str(self.app),
                        TRIM_PKGVAR=str(self.root / "data"),
                        TRIM_PKGTMP=str(self.root / "tmp"),
                        TRIM_TEMP_LOGFILE=str(self.root / "lifecycle.log"),
                        PATH=str(self.bin) + os.pathsep + os.environ["PATH"])

    def executable(self, name, text):
        target = self.bin / name
        target.write_text(text)
        target.chmod(0o700)
        return target

    def child(self, *args, **kwargs):
        child = subprocess.Popen(args, **kwargs)

        def reap():
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)

        self.addCleanup(reap)
        return child

    def config(self):
        commands = self.root / "cmd"
        commands.mkdir()
        for name in ("config_init", "install_init"):
            shutil.copy2(CMD / name, commands / name)
        return subprocess.run(["/bin/sh", str(commands / "config_init")],
                              env=self.env, capture_output=True, text=True, timeout=10)

    def config_preserves_running_services(self):
        # Contain install_init's broad pkill inside this fixture. It still
        # sends a real signal to our child, never another SAG/user process.
        self.executable("pkill", '#!/bin/sh\nkill -TERM "$FIXTURE_PID"\n')
        uds = self.app / "app.sock"
        server = self.child(sys.executable, "-c", """
import socket, sys, time
s = socket.socket(socket.AF_UNIX)
s.bind(sys.argv[1]); s.listen()
while True:
    c, _ = s.accept(); c.sendall(b'live'); c.close()
""", str(uds))
        wait_until(uds.exists)
        inode = uds.stat().st_ino
        self.env["FIXTURE_PID"] = str(server.pid)
        result = self.config()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIsNone(server.poll(), "configuration callback killed the live service")
        self.assertEqual(uds.stat().st_ino, inode, "configuration callback unlinked the live UDS")
        with socket.socket(socket.AF_UNIX) as client:
            client.connect(str(uds))
            self.assertEqual(client.recv(4), b"live")

    def config_preserves_stopped_state(self):
        self.executable("pkill", '#!/bin/sh\nprintf called > "$TRIM_APPDEST/pkill-called"\n')
        result = self.config()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.app / "app.sock").exists())
        self.assertFalse((self.app / "pkill-called").exists(), "configuration must not run install cleanup")
        self.assertFalse((self.root / "data").exists(), "configuration must not start/provision stopped SAG")

    def prepare_main(self):
        # Only substitute unavailable platform runtimes/port discovery.
        # The launcher, /proc identity checks, signals and locking are real.
        source = (CMD / "main").read_text()
        source = source.replace('/var/apps/python312/target/bin/python3', sys.executable)
        source = source.replace('/var/apps/nodejs_v22/target/bin/node', sys.executable)
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        source = source.replace("candidate=3091", "candidate=" + str(port))
        source = source.replace('"$candidate" -le 3191', '"$candidate" -le ' + str(port))
        self.main = self.root / "main"
        self.main.write_text(source)
        self.data = self.root / "data"
        self.run = self.data / "run"
        self.run.mkdir(parents=True)
        (self.root / "tmp").mkdir()
        self.executable("ss", "#!/bin/sh\nexit 0\n")
        self.executable("curl", "#!" + sys.executable + "\n" + '''
import http.client, socket, sys
args = sys.argv[1:]
url = args[-1]
if '--unix-socket' in args:
    address = args[args.index('--unix-socket') + 1]
    conn = http.client.HTTPConnection('localhost', timeout=1)
    conn.sock = socket.socket(socket.AF_UNIX); conn.sock.settimeout(1); conn.sock.connect(address)
else:
    authority = url.split('/')[2]
    conn = http.client.HTTPConnection(authority, timeout=1)
try:
    conn.request('GET', '/' + '/'.join(url.split('/')[3:]))
    response = conn.getresponse(); body = response.read()
    if response.status != 200: sys.exit(22)
    sys.stdout.buffer.write(body)
except OSError:
    sys.exit(7)
finally:
    conn.close()
''')
        web = self.app / "web"
        web.mkdir()
        (web / "server.js").write_text('''
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import os, signal, threading, time
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b'web')
    def log_message(self, *args): pass
def terminate(*args):
    if os.environ.get('FIXTURE_WEB_STOPPING'):
        Path(os.environ['FIXTURE_WEB_STOPPING']).touch()
        while not Path(os.environ['FIXTURE_WEB_RELEASE']).exists(): time.sleep(.02)
    raise SystemExit(0)
signal.signal(signal.SIGTERM, terminate)
server = ThreadingHTTPServer(('127.0.0.1', int(os.environ['PORT'])), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
while True: time.sleep(.1)
''')
        module = self.app / "server/sag_api/fnos"
        module.mkdir(parents=True)
        (module.parent / "__init__.py").touch()
        (module / "__init__.py").touch()
        runtime = self.app / "runtime"
        runtime.mkdir()
        helper = CMD / "stop_workers.py"
        shutil.copy2(helper, self.main.parent / helper.name)
        (module / "cli.py").write_text('''
from http.server import BaseHTTPRequestHandler
import socketserver, sys
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b'{"status":"ok"}')
    def log_message(self, *args): pass
if sys.argv[1] == 'mcp-proxy':
    server = socketserver.TCPServer(('127.0.0.1', int(sys.argv[sys.argv.index('--port') + 1])), Handler)
else:
    socket_path = sys.argv[sys.argv.index('--socket') + 1]
    server = socketserver.UnixStreamServer(socket_path, Handler)
with server: server.serve_forever()
''')

        def clean_daemons():
            # Exact fixture pid records only; never pkill/global process scans.
            for record in self.run.glob("*.pid"):
                try:
                    os.kill(int(record.read_text()), 9)
                except (ValueError, ProcessLookupError, FileNotFoundError):
                    pass

        self.addCleanup(clean_daemons)

    def launch(self, action):
        return self.child("/bin/sh", str(self.main), action, env=self.env,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def action(self, action):
        child = self.launch(action)
        code = child.wait(timeout=45)
        log = Path(self.env["TRIM_TEMP_LOGFILE"])
        self.assertEqual(code, 0, log.read_text() if log.exists() else action)

    def stop_signals_the_validated_pid_only(self):
        self.prepare_main()
        # Reproduce replacement exactly between the validation read and the
        # signal read. Both targets are real, reaped OS processes.
        old = self.child(sys.executable, "-c", "import time; time.sleep(60)", "sag_api.fnos.cli")
        unrelated = self.child(sys.executable, "-c", "import time; time.sleep(60)", "unrelated-fixture")
        threading.Thread(target=old.wait, daemon=True).start()
        threading.Thread(target=unrelated.wait, daemon=True).start()
        record = self.run / "gateway.pid"
        record.write_text(str(old.pid) + "\n")
        self.env.update(FIXTURE_PID_RECORD=str(record), FIXTURE_REPLACEMENT=str(unrelated.pid),
                        FIXTURE_READ_MARKER=str(self.root / "read-marker"))
        self.executable("cat", '''#!/bin/sh
if [ "$#" = 1 ] && [ "$1" = "$FIXTURE_PID_RECORD" ] && [ ! -e "$FIXTURE_READ_MARKER" ]; then
  /bin/cat "$1"
  printf '%s\\n' "$FIXTURE_REPLACEMENT" > "$1"
  touch "$FIXTURE_READ_MARKER"
else
  exec /bin/cat "$@"
fi
''')
        self.action("stop")
        self.assertIsNone(unrelated.poll(), "stop signalled a replacement PID it never validated")
        wait_until(lambda: old.poll() is not None)

    def missing_lock_tool_preserves_live_service(self):
        self.prepare_main()
        # Resolve every utility this failure path needs, while deliberately
        # leaving flock unavailable. Never fall through to an unlocked stop.
        for command in ("mkdir", "id", "tail", "tr", "head", "ls", "dirname"):
            (self.bin / command).symlink_to(shutil.which(command))
        self.env["PATH"] = str(self.bin)
        uds = self.app / "app.sock"
        with socket.socket(socket.AF_UNIX) as listener:
            listener.bind(str(uds))
            listener.listen()
            inode = uds.stat().st_ino
            result = subprocess.run(["/bin/sh", str(self.main), "stop"], env=self.env,
                                    capture_output=True, text=True, timeout=10)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("requires flock", Path(self.env["TRIM_TEMP_LOGFILE"]).read_text())
            self.assertEqual(uds.stat().st_ino, inode)
            with socket.socket(socket.AF_UNIX) as client:
                client.connect(str(uds))

    def unwritable_lock_reports_failure(self):
        self.prepare_main()
        # A directory at the lock path makes shell FD opening really fail,
        # even when the test runner happens to be root inside a container.
        (self.run / "lifecycle.lock").mkdir()
        # macOS has no flock; this executable is not reached, but lets the
        # callback exercise its actual FD-open error and EXIT diagnostics.
        self.executable("flock", "#!/bin/sh\nexit 99\n")
        for action in ("start", "stop", "status", "upgrade-knowledge"):
            log = Path(self.env["TRIM_TEMP_LOGFILE"])
            log.unlink(missing_ok=True)
            result = subprocess.run(["/bin/sh", str(self.main), action], env=self.env,
                                    capture_output=True, text=True, timeout=10)
            self.assertNotEqual(result.returncode, 0)
            self.assertTrue(log.is_file(), action + " must log a fatal lock-open error")
            self.assertIn("SAG Native", log.read_text())
            self.assertIn("lifecycle lock", log.read_text())
            self.assertFalse((self.run / "gateway.pid").exists())

    def upgrade_cleanup_failure_reports_cause(self):
        self.prepare_main()
        self.executable("flock", "#!/bin/sh\nexit 0\n")
        (self.app / "app.sock").mkdir()
        result = subprocess.run(["/bin/sh", str(self.main), "upgrade-knowledge"], env=self.env,
                                capture_output=True, text=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        log = Path(self.env["TRIM_TEMP_LOGFILE"])
        self.assertTrue(log.is_file(), "upgrade failure must reach fnpack's visible log")
        self.assertIn("socket/port", log.read_text())
        self.assertIn("upgrade-knowledge", log.read_text())

    def worker(self, data, *, ignore_term=False):
        module = self.app / "server/sag_api/fnos/worker.py"
        module.write_text('''
import os, signal, sqlite3, time
from pathlib import Path
root = Path(os.environ['SAG_FNOS_DATA_ROOT']); root.mkdir(exist_ok=True)
if os.environ.get('FIXTURE_IGNORE_TERM'): signal.signal(signal.SIGTERM, signal.SIG_IGN)
with sqlite3.connect(root / 'worker.db') as database:
    database.execute('CREATE TABLE IF NOT EXISTS writes (value INTEGER)')
    while True:
        database.execute('INSERT INTO writes VALUES (1)'); database.commit()
        (root / 'worker-ready').touch()
        time.sleep(.02)
''')
        environment = dict(self.env, PYTHONPATH=str(self.app / "server"),
                           SAG_FNOS_DATA_ROOT=str(data))
        if ignore_term:
            environment["FIXTURE_IGNORE_TERM"] = "1"
        worker = self.child(sys.executable, "-m", "sag_api.fnos.worker", env=environment)
        threading.Thread(target=worker.wait, daemon=True).start()
        wait_until(lambda: (data / "worker-ready").exists())
        return worker

    def prepare_pre_payload_main(self):
        self.prepare_main()
        # fnpack delivers new cmd files before replacing app/runtime. Keep
        # the new helper beside main while the installed runtime lacks it.
        old_location = self.app / "runtime/stop_workers.py"
        old_location.unlink(missing_ok=True)
        self.assertTrue((self.main.parent / "stop_workers.py").is_file())
        self.assertFalse(old_location.exists())

    def new_commands_stop_workers_before_payload_replacement(self):
        self.prepare_pre_payload_main()
        orphan = self.worker(self.data)
        self.action("stop")
        wait_until(lambda: orphan.poll() is not None)

    def new_commands_verify_workers_before_upgrade_owner(self):
        self.prepare_pre_payload_main()
        orphan = self.worker(self.data)
        called = self.root / "upgrade-owner-called"
        (self.app / "server/sag_api/fnos/package_upgrade.py").write_text(f'''
from pathlib import Path
stat = Path('/proc/{orphan.pid}/stat')
if stat.exists() and stat.read_text().rpartition(') ')[2].split()[0] not in ('Z', 'X'):
    raise SystemExit('offline upgrade owner was reached while a worker could still write')
Path({str(called)!r}).touch()
''')
        self.action("upgrade-knowledge")
        self.assertTrue(called.is_file(), "upgrade did not reach its verified offline owner")
        wait_until(lambda: orphan.poll() is not None)

    def start_reaps_workers_left_by_dead_gateway(self):
        self.prepare_main()
        environment = dict(self.env, PYTHONPATH=str(self.app / "server"))
        gateway = self.child(sys.executable, "-m", "sag_api.fnos.cli", "gateway",
                             "--socket", str(self.app / "app.sock"), env=environment)
        wait_until(lambda: (self.app / "app.sock").is_socket())
        (self.run / "gateway.pid").write_text(str(gateway.pid))
        orphan = self.worker(self.data)
        unrelated = self.worker(self.root / "other-package")
        lookalike = self.child(sys.executable, "-c", "import time; time.sleep(60)",
                               "-m", "sag_api.fnos.worker",
                               env=dict(self.env, SAG_FNOS_DATA_ROOT=str(self.data)))
        gateway.kill()
        gateway.wait(timeout=5)
        self.assertIsNone(orphan.poll(), "gateway exit must leave the old writer alive for this regression")
        called = self.root / "cold-gateway-started"
        module = self.app / "server/sag_api/fnos/cli.py"
        module.write_text(f'''
from pathlib import Path
import sys
if sys.argv[1] == 'gateway':
    stat = Path('/proc/{orphan.pid}/stat')
    if stat.exists() and stat.read_text().rpartition(') ')[2].split()[0] not in ('Z', 'X'):
        raise SystemExit('gateway was started while an old tenant could still write')
    Path({str(called)!r}).touch()
''' + module.read_text())
        self.action("start")
        self.assertTrue(called.is_file(), "startup never reached the cold gateway boundary")
        self.assertIsNotNone(orphan.poll(), "startup returned with its old tenant writer alive")
        self.assertIsNone(unrelated.poll(), "startup killed another DATA worker")
        self.assertIsNone(lookalike.poll(), "startup matched a worker name outside the module argument")
        import sqlite3
        with sqlite3.connect(self.data / "worker.db") as database:
            before = database.execute('SELECT count(*) FROM writes').fetchone()[0]
            time.sleep(.15)
            after = database.execute('SELECT count(*) FROM writes').fetchone()[0]
        self.assertEqual(before, after, "old tenant wrote after the new gateway started")
        self.action("stop")

    def healthy_start_keeps_active_tenant_workers(self):
        self.prepare_main()
        self.action("start")
        recorded = {name: (self.run / name).read_text() for name in ("gateway.pid", "web.pid")}
        inode = (self.app / "app.sock").stat().st_ino
        worker = self.worker(self.data)
        import sqlite3
        with sqlite3.connect(self.data / "worker.db") as database:
            before = database.execute('SELECT count(*) FROM writes').fetchone()[0]
        self.action("start")
        self.assertIsNone(worker.poll(), "idempotent startup killed a healthy gateway's active tenant")
        self.assertEqual({name: (self.run / name).read_text() for name in recorded}, recorded)
        self.assertEqual((self.app / "app.sock").stat().st_ino, inode)
        with sqlite3.connect(self.data / "worker.db") as database:
            after = database.execute('SELECT count(*) FROM writes').fetchone()[0]
        self.assertGreater(after, before, "healthy tenant stopped committing during idempotent startup")
        self.action("stop")

    def start_reaps_workers_if_gateway_dies_while_web_becomes_ready(self):
        self.prepare_main()
        environment = dict(self.env, PYTHONPATH=str(self.app / "server"))
        gateway = self.child(sys.executable, "-m", "sag_api.fnos.cli", "gateway",
                             "--socket", str(self.app / "app.sock"), env=environment)
        threading.Thread(target=gateway.wait, daemon=True).start()
        wait_until(lambda: (self.app / "app.sock").is_socket())
        (self.run / "gateway.pid").write_text(str(gateway.pid))
        orphan = self.worker(self.data)
        marker = self.root / "gateway-killed-during-web-probe"
        curl = self.bin / "curl"
        curl.write_text(curl.read_text().replace("url = args[-1]", f'''
url = args[-1]
if '--unix-socket' not in args:
    from pathlib import Path
    import os, signal
    marker = Path({str(marker)!r})
    if not marker.exists():
        os.kill({gateway.pid}, signal.SIGKILL)
        marker.touch()
'''))
        module = self.app / "server/sag_api/fnos/cli.py"
        module.write_text(f'''
from pathlib import Path
import sys
if sys.argv[1] == 'gateway':
    stat = Path('/proc/{orphan.pid}/stat')
    if stat.exists() and stat.read_text().rpartition(') ')[2].split()[0] not in ('Z', 'X'):
        raise SystemExit('replacement gateway was started while an old tenant could still write')
''' + module.read_text())
        self.action("start")
        self.assertTrue(marker.is_file(), "gateway did not exit inside the Web readiness interval")
        self.assertIsNotNone(orphan.poll(), "late gateway exit bypassed tenant worker cleanup")
        self.action("stop")

    def start_refuses_when_old_worker_identity_cannot_be_pinned(self):
        self.prepare_main()
        orphan = self.worker(self.data)
        helper = self.main.parent / "stop_workers.py"
        helper.write_text("import os\ndef unavailable(*args, **kwargs): raise OSError('pidfd unavailable')\n"
                          "os.pidfd_open = unavailable\n" + helper.read_text().replace(
                              "from __future__ import annotations\n", ""))
        result = subprocess.run(["/bin/sh", str(self.main), "start"], env=self.env,
                                capture_output=True, text=True, timeout=10)
        self.assertNotEqual(result.returncode, 0, "startup ignored an unverified old tenant writer")
        self.assertIsNone(orphan.poll(), "unverified worker must not be signalled")
        self.assertFalse((self.run / "gateway.pid").exists(), "startup published a gateway before safe cleanup")
        self.assertFalse((self.run / "web.pid").exists(), "startup launched Web before safe cleanup")
        self.assertFalse((self.app / "app.sock").exists())
        self.assertIn("worker", Path(self.env["TRIM_TEMP_LOGFILE"]).read_text())

    def stop_reaps_legacy_orphan_workers(self):
        self.prepare_main()
        # These workers have no parent-death hooks or lifecycle records,
        # exactly as packages released before this fix.
        orphan = self.worker(self.data, ignore_term=True)
        # An active request can outlive main's 15-second gateway grace.
        # Force that branch so the worker cannot rely on supervisor.close.
        gateway_ready = self.root / "gateway-ready"
        gateway = self.child(sys.executable, "-c",
                             "import signal,time; from pathlib import Path; "
                             "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                             f"Path({str(gateway_ready)!r}).touch(); time.sleep(60)",
                             "sag_api.fnos.cli")
        threading.Thread(target=gateway.wait, daemon=True).start()
        wait_until(gateway_ready.exists)
        (self.run / "gateway.pid").write_text(str(gateway.pid))
        other_root = self.root / "other-package"
        unrelated = self.worker(other_root)
        # A module name in arbitrary program arguments is not a worker.
        lookalike = self.child(sys.executable, "-c", "import time; time.sleep(60)",
                               "-m", "sag_api.fnos.worker",
                               env=dict(self.env, SAG_FNOS_DATA_ROOT=str(self.data)))
        self.action("stop")
        self.assertIsNotNone(gateway.poll(), "stop returned before the forced gateway exit")
        wait_until(lambda: orphan.poll() is not None)
        self.assertIsNone(unrelated.poll(), "stop killed another package's tenant worker")
        self.assertIsNone(lookalike.poll(), "stop matched a worker name outside the module argument")
        import sqlite3
        with sqlite3.connect(self.data / "worker.db") as database:
            before = database.execute('SELECT count(*) FROM writes').fetchone()[0]
            time.sleep(.15)
            after = database.execute('SELECT count(*) FROM writes').fetchone()[0]
        self.assertEqual(before, after, "stopped worker committed data after stop returned")

    def upgrade_refuses_when_worker_identity_cannot_be_pinned(self):
        self.prepare_main()
        orphan = self.worker(self.data)
        # Execute the real helper with only the unavailable kernel primitive
        # substituted; a failed identity pin must never fall back to kill(pid).
        helper = self.main.parent / "stop_workers.py"
        if helper.exists():
            helper.write_text("import os\ndef unavailable(*args, **kwargs): raise OSError('pidfd unavailable')\n"
                              "os.pidfd_open = unavailable\n" + helper.read_text().replace(
                                  "from __future__ import annotations\n", ""))
        result = subprocess.run(["/bin/sh", str(self.main), "upgrade-knowledge"], env=self.env,
                                capture_output=True, text=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertIsNone(orphan.poll(), "unverified process must not be signalled")
        self.assertIn("worker", Path(self.env["TRIM_TEMP_LOGFILE"]).read_text())

    def concurrent_restart_keeps_the_new_socket(self):
        self.prepare_main()
        stopping = self.root / "web-stopping"
        release = self.root / "release-web"
        self.env.update(FIXTURE_WEB_STOPPING=str(stopping), FIXTURE_WEB_RELEASE=str(release))
        self.addCleanup(lambda: release.touch())
        self.action("start")
        stop = self.launch("stop")
        wait_until(stopping.exists, timeout=20)
        start = self.launch("start")
        time.sleep(1.5)
        try:
            self.assertIsNone(start.poll(), "start completed while the previous stop still owned the lifecycle")
            self.assertFalse((self.run / "gateway.pid").exists(),
                             "start published a new gateway before the old stop completed")
        finally:
            release.touch()
        self.assertEqual(stop.wait(timeout=35), 0)
        self.assertEqual(start.wait(timeout=35), 0)
        self.action("status")
        self.assertTrue((self.app / "app.sock").is_socket(), "old stop removed the new gateway socket")
        self.action("stop")

    def concurrent_starts_reuse_one_service_set(self):
        self.prepare_main()
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            self.env["TRIM_SERVICE_PORT"] = str(reservation.getsockname()[1])
        first = self.launch("start")
        second = self.launch("start")
        self.assertEqual(first.wait(timeout=35), 0)
        self.assertEqual(second.wait(timeout=35), 0)
        self.action("status")
        recorded = {int((self.run / name).read_text()) for name in ("gateway.pid", "web.pid", "mcp-public.pid")}
        targets = {str(self.app / "app.sock").encode(), str(self.app / "web/server.js").encode()}
        live = set()
        for process in Path("/proc").iterdir():
            if not process.name.isdecimal():
                continue
            try:
                arguments = (process / "cmdline").read_bytes().split(b"\0")
            except (FileNotFoundError, PermissionError, ProcessLookupError):
                continue
            if targets.intersection(arguments):
                live.add(int(process.name))
        self.assertEqual(live, recorded, "parallel starts leaked or replaced a fixture daemon")
        self.action("stop")


unittest.main(argv=[sys.argv[0], "Lifecycle." + CASE])
