import io
import json

import pytest

from dsh_sag_runtime.__main__ import parse_args, run_stdio
from dsh_sag_runtime.server import RuntimeServer
from test_server import FakePool, FakeRead, FakeSearch


def test_cli_requires_explicit_env_file_and_namespace() -> None:
    args = parse_args(["--env-file", "/config/sag.env", "--namespace", "product-docs"])
    assert args.env_file == "/config/sag.env"
    assert args.namespace == ["product-docs"]


@pytest.mark.asyncio
async def test_stdio_writes_only_json_rpc_to_stdout_and_closes_on_eof() -> None:
    pool = FakePool()
    server = RuntimeServer(pool, FakeSearch(), FakeRead(), ("a",), engine_version="0.14.0")
    reader = io.BytesIO(b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"1.0"}}\n')
    writer = io.BytesIO()
    diagnostics = io.StringIO()

    await run_stdio(server, reader, writer, diagnostics)

    lines = writer.getvalue().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["result"]["engineVersion"] == "0.14.0"
    assert diagnostics.getvalue() == ""
    assert pool.closed == 1


@pytest.mark.asyncio
async def test_stdio_keeps_malformed_frame_diagnostics_off_stdout() -> None:
    server = RuntimeServer(FakePool(), FakeSearch(), FakeRead(), ("a",), engine_version="0.14.0")
    writer = io.BytesIO()
    diagnostics = io.StringIO()

    await run_stdio(server, io.BytesIO(b"not-json\n"), writer, diagnostics)

    assert writer.getvalue() == b""
    assert "rejected a frame" in diagnostics.getvalue()
