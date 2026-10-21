"""Stop this fnOS package's tenant workers, including older orphan workers.

Only pidfds receive signals. An unreadable candidate or unavailable pidfd is a
reason to abort the cold backup, never a reason to fall back to a numeric PID.
"""

from __future__ import annotations

import argparse
import errno
import os
from pathlib import Path
import select
import signal
import sys
import time


class WorkerStopError(RuntimeError):
    pass


def _read_at(directory: int, name: str) -> bytes:
    descriptor = os.open(name, os.O_RDONLY | os.O_CLOEXEC, dir_fd=directory)
    with os.fdopen(descriptor, "rb") as stream:
        return stream.read()


def _state(directory: int) -> bytes:
    return _read_at(directory, "stat").rpartition(b") ")[2].split()[0]


def _worker_pidfds(root: Path) -> list[tuple[int, int]]:
    """Pin matching processes through a proc directory and a kernel pidfd."""
    descriptors: list[tuple[int, int]] = []
    try:
        for entry in Path("/proc").iterdir():
            if not entry.name.isdecimal():
                continue
            directory = -1
            descriptor = -1
            try:
                directory = os.open(entry, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
                # All SAG daemons run as the package user. Never inspect or
                # signal another user's processes on this NAS.
                if os.fstat(directory).st_uid != os.geteuid():
                    continue
                if _state(directory) in (b"Z", b"X"):
                    continue
                arguments = _read_at(directory, "cmdline").rstrip(b"\0").split(b"\0")
                if arguments[1:3] != [b"-m", b"sag_api.fnos.worker"]:
                    continue
                environment = _read_at(directory, "environ").split(b"\0")
                roots = [value.partition(b"=")[2] for value in environment
                         if value.startswith(b"SAG_FNOS_DATA_ROOT=")]
                if len(roots) != 1:
                    raise WorkerStopError(f"worker pid={entry.name} has no unambiguous DATA identity")
                candidate = Path(os.fsdecode(roots[0]))
                if not candidate.is_absolute():
                    raise WorkerStopError(f"worker pid={entry.name} has a relative DATA identity")
                if candidate.resolve() != root:
                    continue
                status = _read_at(directory, "status").splitlines()
                owners = next(line.split()[1:] for line in status if line.startswith(b"Uid:"))
                if owners != [str(os.geteuid()).encode()] * 4:
                    raise WorkerStopError(f"worker pid={entry.name} has unexpected process ownership")
                if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
                    raise WorkerStopError("kernel/Python pidfd support is unavailable")
                descriptor = os.pidfd_open(int(entry.name))
                # A /proc/<pid> directory FD remains tied to its original
                # process. Re-reading it after pidfd_open detects exit/reuse
                # without the race inherent in reopening /proc/<numeric PID>.
                if _state(directory) in (b"Z", b"X"):
                    continue
                descriptors.append((int(entry.name), descriptor))
                descriptor = -1
            except OSError as error:
                if error.errno not in (errno.ENOENT, errno.ESRCH):
                    raise WorkerStopError(f"cannot verify worker pid={entry.name}: {error}") from error
            finally:
                if directory >= 0:
                    os.close(directory)
                if descriptor >= 0:
                    os.close(descriptor)
        return descriptors
    except BaseException:
        for _, descriptor in descriptors:
            os.close(descriptor)
        raise


def _wait(descriptors: list[tuple[int, int]], seconds: float) -> list[tuple[int, int]]:
    deadline = time.monotonic() + seconds
    remaining = descriptors
    while remaining:
        ready, _, _ = select.select([descriptor for _, descriptor in remaining], [], [],
                                    max(0, deadline - time.monotonic()))
        remaining = [(pid, descriptor) for pid, descriptor in remaining if descriptor not in ready]
        if time.monotonic() >= deadline:
            break
    return remaining


def stop_workers(root: Path) -> None:
    if sys.platform != "linux" or not Path("/proc/self/stat").is_file():
        raise WorkerStopError("Linux /proc process verification is unavailable")
    descriptors = _worker_pidfds(root.resolve())
    try:
        for pid, descriptor in descriptors:
            try:
                signal.pidfd_send_signal(descriptor, signal.SIGTERM)
            except ProcessLookupError:
                pass
            except OSError as error:
                raise WorkerStopError(f"cannot stop worker pid={pid}: {error}") from error
        remaining = _wait(descriptors, 15)
        for pid, descriptor in remaining:
            try:
                signal.pidfd_send_signal(descriptor, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except OSError as error:
                raise WorkerStopError(f"cannot force-stop worker pid={pid}: {error}") from error
        remaining = _wait(remaining, 5)
        if remaining:
            raise WorkerStopError("workers did not exit: " + ", ".join(str(pid) for pid, _ in remaining))
    finally:
        for _, descriptor in descriptors:
            os.close(descriptor)
    # Gateway must already be stopped under the lifecycle lock. A second
    # complete scan catches any worker launched during its final shutdown.
    remaining = _worker_pidfds(root.resolve())
    for _, descriptor in remaining:
        os.close(descriptor)
    if remaining:
        raise WorkerStopError("new workers appeared during stop; retry after checking package processes")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    try:
        if not args.root.is_absolute():
            raise WorkerStopError("DATA root must be absolute")
        stop_workers(args.root)
    except (WorkerStopError, OSError, ValueError) as error:
        print(f"SAG Native cannot confirm tenant workers stopped: {error}. "
              "Keep DATA and backups intact; inspect package processes and retry before upgrading.",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
