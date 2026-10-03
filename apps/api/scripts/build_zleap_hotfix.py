"""Rebuild the pinned upstream wheel with the General-number display fix.

This changes a build artifact, never an installed environment or a workbook.
The upstream SHA-256 is also recorded in uv.lock and on PyPI.
"""

from __future__ import annotations

import argparse
import base64
import copy
import csv
import hashlib
import io
from pathlib import Path
from zipfile import ZipFile

UPSTREAM_SHA256 = "2beee6f89a66af20adda6c3e96321a99113de6f682d2f2a023d6f50297bd41a9"
UPSTREAM_DIST_INFO = "zleap_sag-0.13.0.dist-info/"
HOTFIX_VERSION = "0.13.0+sag.1"
HOTFIX_DIST_INFO = f"zleap_sag-{HOTFIX_VERSION}.dist-info/"
HOTFIX_FILENAME = f"zleap_sag-{HOTFIX_VERSION}-py3-none-any.whl"
READER_PATH = "zleap/sag/pipeline/spreadsheets/reader.py"
_BEFORE = '        return format(value, "f").rstrip("0").rstrip(".") or "0"\n'
_AFTER = (
    '        rendered = format(value, "f")\n'
    '        return rendered.rstrip("0").rstrip(".") if "." in rendered else rendered\n'
)


def build_hotfix(upstream: Path, output: Path) -> None:
    if hashlib.sha256(upstream.read_bytes()).hexdigest() != UPSTREAM_SHA256:
        raise ValueError("upstream wheel does not match the pinned SHA-256")
    if output.exists():
        raise FileExistsError(f"refusing to overwrite {output}")

    records: list[tuple[str, str, str]] = []
    output.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(upstream) as original, ZipFile(output, "w") as patched:
        reader = original.read(READER_PATH).decode("utf-8")
        if reader.count(_BEFORE) != 1:
            raise ValueError("expected exactly one General display expression")
        for entry in original.infolist():
            if entry.filename == UPSTREAM_DIST_INFO + "RECORD":
                continue
            content = original.read(entry)
            if entry.filename == READER_PATH:
                content = reader.replace(_BEFORE, _AFTER).encode("utf-8")
            elif entry.filename == UPSTREAM_DIST_INFO + "METADATA":
                metadata = content.decode("utf-8")
                version = "Version: 0.13.0\n"
                if metadata.count(version) != 1:
                    raise ValueError("unexpected upstream distribution version")
                content = metadata.replace(version, f"Version: {HOTFIX_VERSION}\n").encode("utf-8")
            info = copy.copy(entry)
            if info.filename.startswith(UPSTREAM_DIST_INFO):
                info.filename = HOTFIX_DIST_INFO + info.filename[len(UPSTREAM_DIST_INFO):]
            patched.writestr(info, content)
            digest = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b"=")
            records.append((info.filename, f"sha256={digest.decode('ascii')}", str(len(content))))

        record_path = HOTFIX_DIST_INFO + "RECORD"
        records.append((record_path, "", ""))
        record_text = io.StringIO(newline="")
        csv.writer(record_text, lineterminator="\n").writerows(records)
        record_info = copy.copy(original.getinfo(UPSTREAM_DIST_INFO + "RECORD"))
        record_info.filename = record_path
        patched.writestr(record_info, record_text.getvalue().encode("utf-8"))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("upstream", type=Path, help="official zleap-sag 0.13.0 wheel")
    parser.add_argument(
        "--output", type=Path,
        default=Path(__file__).resolve().parents[1] / "vendor" / HOTFIX_FILENAME,
    )
    args = parser.parse_args()
    build_hotfix(args.upstream, args.output)
    print(f"{hashlib.sha256(args.output.read_bytes()).hexdigest()}  {args.output.name}")


if __name__ == "__main__":
    main()
