from __future__ import annotations

import os
import time
from pathlib import Path

_WINDOWS_TRANSIENT_REPLACE_ERRORS = frozenset({5, 32, 33})
_WINDOWS_REPLACE_RETRY_DELAYS = (
    0.1,
    0.2,
    0.4,
    0.8,
    1.0,
    1.0,
    1.0,
    1.0,
    1.0,
    1.0,
    1.0,
    1.0,
    1.0,
)


def is_transient_windows_replace_error(error: BaseException) -> bool:
    return getattr(error, "winerror", None) in _WINDOWS_TRANSIENT_REPLACE_ERRORS


def replace_directory(source: Path, destination: Path) -> None:
    for delay in _WINDOWS_REPLACE_RETRY_DELAYS:
        try:
            os.replace(source, destination)
            return
        except OSError as error:
            if not is_transient_windows_replace_error(error):
                raise
            time.sleep(delay)
    os.replace(source, destination)
