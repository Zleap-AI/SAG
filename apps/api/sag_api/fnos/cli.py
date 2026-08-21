"""Native fnOS gateway command-line entrypoint."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

import uvicorn

from sag_api.fnos.gateway import create_gateway_app
from sag_api.fnos.identity import InternalIdentitySigner, derive_fnos_internal_key
from sag_api.fnos.supervisor import WorkerSupervisor


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    gateway = commands.add_parser("gateway")
    gateway.add_argument("--socket", required=True, type=Path)
    gateway.add_argument("--web-origin", required=True)
    public_mcp = commands.add_parser("mcp-proxy")
    public_mcp.add_argument("--socket", required=True, type=Path)
    public_mcp.add_argument("--host", default="0.0.0.0")
    public_mcp.add_argument("--port", required=True, type=int)
    args = parser.parse_args(argv)
    if args.command == "mcp-proxy":
        from sag_api.fnos.public_mcp import create_public_mcp_app

        uvicorn.run(
            create_public_mcp_app(args.socket),
            host=args.host,
            port=args.port,
            proxy_headers=False,
            forwarded_allow_ips="",
        )
        return
    data_root = Path(os.environ["SAG_FNOS_DATA_ROOT"])
    temp_root = Path(os.environ["SAG_FNOS_TEMP_ROOT"])
    secret_file = Path(os.environ["SAG_FNOS_INTERNAL_SECRET_FILE"])
    signer = InternalIdentitySigner.from_file(secret_file)
    app = create_gateway_app(
        WorkerSupervisor(data_root, temp_root, identity_signer=signer),
        signer,
        args.web_origin,
        mcp_routing_key=derive_fnos_internal_key(secret_file, b"sag-fnos-mcp-routing-v1"),
    )
    uvicorn.run(app, uds=str(args.socket), proxy_headers=False, forwarded_allow_ips="")


if __name__ == "__main__":
    main()
