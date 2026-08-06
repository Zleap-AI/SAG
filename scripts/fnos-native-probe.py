#!/usr/bin/env python3
"""Verify the native fnOS Python runtime and expose its result over UDS."""

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path


DEPENDENCIES = ("lancedb", "pyarrow", "onnxruntime", "numpy", "uvloop", "orjson")
GATEWAY_HEADERS = ("x-trim-userid", "x-trim-username", "x-trim-isadmin")


def parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="run the native probe over a Unix socket")
    serve.add_argument("--socket", required=True, type=Path)
    serve.add_argument("--output", required=True, type=Path)
    return parser.parse_args(argv)


def initial_result():
    return {
        "python": platform.python_version(),
        "machine": platform.machine(),
        "imports": {name: False for name in DEPENDENCIES},
        "lancedb_roundtrip": False,
        "uds_http": False,
        "gateway_headers": {name: None for name in GATEWAY_HEADERS},
        "status": "fail",
        "errors": [],
    }


def write_json_atomically(output, result, orjson_module=None):
    output.parent.mkdir(parents=True, exist_ok=True)
    if orjson_module:
        encoded = orjson_module.dumps(result, option=orjson_module.OPT_SORT_KEYS)
    else:
        encoded = json.dumps(result, sort_keys=True, separators=(",", ":")).encode("utf-8")
    temporary = output.with_name(f".{output.name}.{os.getpid()}.tmp")
    temporary.write_bytes(encoded)
    os.replace(temporary, output)


def load_native_dependencies(result):
    vendor = Path(__file__).with_name("vendor")
    if vendor.is_dir():
        sys.path.insert(0, str(vendor))

    modules = {}
    try:
        import lancedb
        modules["lancedb"] = lancedb
        result["imports"]["lancedb"] = True
    except Exception as error:  # Native extension failures are probe results.
        result["errors"].append(f"lancedb import: {error}")
    try:
        import pyarrow
        modules["pyarrow"] = pyarrow
        result["imports"]["pyarrow"] = True
    except Exception as error:
        result["errors"].append(f"pyarrow import: {error}")
    try:
        import onnxruntime
        modules["onnxruntime"] = onnxruntime
        result["imports"]["onnxruntime"] = True
    except Exception as error:
        result["errors"].append(f"onnxruntime import: {error}")
    try:
        import numpy
        modules["numpy"] = numpy
        result["imports"]["numpy"] = True
    except Exception as error:
        result["errors"].append(f"numpy import: {error}")
    try:
        import uvloop
        modules["uvloop"] = uvloop
        result["imports"]["uvloop"] = True
    except Exception as error:
        result["errors"].append(f"uvloop import: {error}")
    try:
        import orjson
        modules["orjson"] = orjson
        result["imports"]["orjson"] = True
    except Exception as error:
        result["errors"].append(f"orjson import: {error}")
    return modules


def check_shared_objects(result):
    vendor = Path(__file__).with_name("vendor")
    ldd = shutil.which("ldd")
    if not ldd:
        result["errors"].append("ldd is unavailable")
        return False
    shared_objects = tuple(vendor.rglob("*.so")) if vendor.is_dir() else ()
    if not shared_objects:
        result["errors"].append("vendor contains no shared objects")
        return False
    passed = True
    for shared_object in shared_objects:
        checked = subprocess.run([ldd, str(shared_object)], capture_output=True, text=True, check=False)
        details = f"{checked.stdout}\n{checked.stderr}".lower()
        if checked.returncode != 0 or "not found" in details:
            result["errors"].append(f"ldd {shared_object.relative_to(vendor)}: {details.strip()}")
            passed = False
    return passed


def check_lancedb_roundtrip(modules, result):
    with tempfile.TemporaryDirectory(prefix="sag-native-probe-") as temporary:
        database = Path(temporary) / "vectors"
        connection = modules["lancedb"].connect(str(database))
        table = connection.create_table(
            "probe",
            data=[
                {"id": "nearest", "vector": [0.0, 0.0]},
                {"id": "far", "vector": [10.0, 10.0]},
            ],
        )
        matches = table.search([0.1, 0.1]).limit(1).to_list()
        result["lancedb_roundtrip"] = bool(matches and matches[0].get("id") == "nearest")
        if not result["lancedb_roundtrip"]:
            result["errors"].append("LanceDB nearest-neighbor query did not return the expected vector")


def check_uds_http(result):
    import httpx
    from fastapi import FastAPI
    import uvicorn

    with tempfile.TemporaryDirectory(prefix="sag-native-uds-") as temporary:
        socket = Path(temporary) / "probe.sock"
        app = FastAPI()

        @app.get("/ready")
        async def ready():
            return {"ready": True}

        server = uvicorn.Server(uvicorn.Config(app, uds=str(socket), log_level="error", access_log=False))
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 10
            while not socket.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            if not socket.exists():
                raise RuntimeError("Uvicorn did not create its Unix socket")
            with httpx.Client(transport=httpx.HTTPTransport(uds=str(socket)), base_url="http://localhost") as client:
                response = client.get("/ready")
            result["uds_http"] = response.status_code == 200 and response.json() == {"ready": True}
            if not result["uds_http"]:
                result["errors"].append("UDS HTTP readiness response was invalid")
        finally:
            server.should_exit = True
            thread.join(timeout=10)
            socket.unlink(missing_ok=True)


def run_initial_checks(output):
    result = initial_result()
    modules = load_native_dependencies(result)
    check_shared_objects(result)
    if all(result["imports"].values()):
        try:
            check_lancedb_roundtrip(modules, result)
        except Exception as error:
            result["errors"].append(f"LanceDB roundtrip: {error}")
        try:
            check_uds_http(result)
        except Exception as error:
            result["errors"].append(f"UDS HTTP: {error}")
    result["status"] = "fail"
    write_json_atomically(output, result, modules.get("orjson"))
    return result, modules.get("orjson")


def native_checks_pass(result):
    return (
        all(result["imports"].values())
        and result["lancedb_roundtrip"]
        and result["uds_http"]
        and not result["errors"]
    )


def gateway_headers_captured(result):
    return all(result["gateway_headers"].get(header) for header in GATEWAY_HEADERS)


def update_gateway_status(result):
    error = "gateway identity headers were not captured"
    result["errors"] = [item for item in result["errors"] if item != error]
    if not gateway_headers_captured(result):
        result["errors"].append(error)
        result["status"] = "fail"
        return False
    result["status"] = "pass" if native_checks_pass(result) else "fail"
    return result["status"] == "pass"


def serve(socket, output, result, orjson_module):
    from fastapi import FastAPI, Request
    import uvicorn

    socket.parent.mkdir(parents=True, exist_ok=True)
    socket.unlink(missing_ok=True)
    lock = threading.Lock()
    app = FastAPI()

    @app.get("/probe")
    @app.get("/app/sag/probe")
    async def probe(request: Request):
        with lock:
            result["gateway_headers"] = {
                header: request.headers.get(header) for header in GATEWAY_HEADERS
            }
            update_gateway_status(result)
            write_json_atomically(output, result, orjson_module)
            return result

    try:
        uvicorn.run(app, uds=str(socket), log_level="error", access_log=False)
    finally:
        socket.unlink(missing_ok=True)
    return gateway_headers_captured(result) and result["status"] == "pass"


def main(argv=None):
    args = parse_args(argv)
    result, orjson_module = run_initial_checks(args.output)
    if not native_checks_pass(result):
        return 1
    if serve(args.socket, args.output, result, orjson_module):
        return 0
    update_gateway_status(result)
    write_json_atomically(args.output, result, orjson_module)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
