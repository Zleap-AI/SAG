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



unittest.main(argv=[sys.argv[0], "Lifecycle." + CASE])
