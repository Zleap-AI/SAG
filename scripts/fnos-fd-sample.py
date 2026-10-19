#!/usr/bin/env python3
"""Manually sample one Linux process's FD counts without printing FD targets."""

from __future__ import annotations

import argparse
import errno
import json
import math
import os
import re
import stat
import sys
import time
from pathlib import Path


class ProcessReplacedError(Exception):
    """The selected PID now identifies a different process."""


def positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a positive integer") from error
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def positive_interval(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be positive and finite") from error
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("must be positive and finite")
    return parsed


def read_identity(process_root: Path) -> int:
    # comm (field 2) may contain spaces and closing parentheses. Only field
    # 22, starttime, is retained; names, command lines and environments are not.
    fields = process_root.joinpath("stat").read_text().rpartition(")")[2].split()
    return int(fields[19])


def classify_fd(descriptor: Path, target: str) -> str:
    for prefix, category in [("socket:", "socket"), ("pipe:", "pipe"), ("anon_inode:", "anon_inode")]:
        if target.startswith(prefix):
            return category
    target = target.removesuffix(" (deleted)")
    if any(part.lower().endswith(".lance") for part in Path(target).parts):
        return "lancedb"
    if re.search(r"\.(db|sqlite|sqlite3)(-(wal|shm|journal))?$", Path(target).name, re.IGNORECASE):
        return "sqlite"
    return "regular" if stat.S_ISREG(descriptor.stat().st_mode) else "other"


def sample_process(pid: int, *, proc_root: Path = Path("/proc"), expected_start: int | None = None) -> dict:
    process_root = proc_root / str(pid)
    started = read_identity(process_root)
    if expected_start is not None and expected_start != started:
        raise ProcessReplacedError
    limits = None
    for line in process_root.joinpath("limits").read_text().splitlines():
        if line.startswith("Max open files"):
            soft, hard = line.split()[3:5]
            limits = {"soft": int(soft) if soft != "unlimited" else soft,
                      "hard": int(hard) if hard != "unlimited" else hard}
            break
    if limits is None:
        raise ValueError("missing NOFILE limits")
    counts = dict.fromkeys(["socket", "pipe", "anon_inode", "sqlite", "lancedb", "regular", "other"], 0)
    unresolved = {"vanished": 0, "permission_denied": 0}
    # Enumerate only the explicitly selected PID, never other processes.
    descriptors = [entry for entry in process_root.joinpath("fd").iterdir() if entry.name.isdecimal()]
    for descriptor in descriptors:
        try:
            category = classify_fd(descriptor, os.readlink(descriptor))
        except FileNotFoundError:
            unresolved["vanished"] += 1
            category = "other"
        except PermissionError:
            unresolved["permission_denied"] += 1
            category = "other"
        counts[category] += 1
    if read_identity(process_root) != started:
        raise ProcessReplacedError
    return {"pid": pid, "start_time_ticks": started, "total": len(descriptors),
            "counts": counts, "nofile": limits, "unresolved": unresolved}


def main(argv: list[str] | None = None, *, proc_root: Path = Path("/proc")) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pid", type=positive_int, help="one target PID")
    parser.add_argument("--count", type=positive_int, default=5, help="finite sample count (default: 5)")
    parser.add_argument(
        "--interval", type=positive_interval, default=10.0, help="seconds between samples (default: 10)"
    )
    args = parser.parse_args(argv)
    expected_start = None
    for index in range(args.count):
        row = {"timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "sample": index + 1, "pid": args.pid}
        try:
            if sys.platform != "linux" and proc_root == Path("/proc"):
                row["status"] = "unsupported_platform"
            else:
                row.update(sample_process(args.pid, proc_root=proc_root, expected_start=expected_start))
                expected_start = row["start_time_ticks"]
                row["status"] = "ok"
        except ProcessReplacedError:
            row.update(status="process_replaced", start_time_ticks=expected_start)
        except OSError as error:
            row["status"] = {
                errno.ENOENT: "process_gone", errno.ESRCH: "process_gone",
                errno.EACCES: "permission_denied", errno.EPERM: "permission_denied",
            }.get(error.errno, "read_error")
        except (ValueError, IndexError):
            row["status"] = "invalid_proc_data"
        print(json.dumps(row), flush=True)
        if row["status"] != "ok":
            return 1
        if index + 1 < args.count:
            time.sleep(args.interval)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130) from None
