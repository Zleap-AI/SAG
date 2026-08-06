"""Native fnOS gateway command-line entrypoint."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

import uvicorn

from sag_api.fnos.gateway import create_gateway_app
from sag_api.fnos.identity import InternalIdentitySigner
from sag_api.fnos.supervisor import WorkerSupervisor


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    gateway = commands.add_parser("gateway")
    gateway.add_argument("--socket", required=True, type=Path)
    gateway.add_argument("--web-origin", required=True)
    args = parser.parse_args(argv)
    if args.command != "gateway":
        raise SystemExit(2)
    data_root = Path(os.environ["SAG_FNOS_DATA_ROOT"])
    temp_root = Path(os.environ["SAG_FNOS_TEMP_ROOT"])
    signer = InternalIdentitySigner.from_file(Path(os.environ["SAG_FNOS_INTERNAL_SECRET_FILE"]))
    app = create_gateway_app(WorkerSupervisor(data_root, temp_root, identity_signer=signer), signer, args.web_origin)
    uvicorn.run(app, uds=str(args.socket), proxy_headers=False, forwarded_allow_ips="")


if __name__ == "__main__":
    main()
