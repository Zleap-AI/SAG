"""Exercise the shipped SDK/storage under an explicitly constrained x86 CPU.

Run write and reopen in separate processes. Embeddings come from a deterministic
loopback fixture; this tests native storage compatibility, not model quality.
"""

import argparse
import asyncio
import ctypes
import json
import mmap
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.metadata import version
from pathlib import Path


def cpu_features() -> dict[str, bool]:
    # SysV x86-64: cpuid(leaf, subleaf, uint32[4]); preserve callee-owned RBX.
    # /proc/cpuinfo reports the ARM host under qemu-user, so query CPUID itself.
    code = bytes.fromhex(
        "53 49 89 d0 89 f8 89 f1 0f a2 41 89 00 41 89 58 04 41 89 48 08 41 89 50 0c 5b c3"
    )
    with mmap.mmap(-1, len(code), prot=mmap.PROT_READ | mmap.PROT_WRITE | mmap.PROT_EXEC) as memory:
        memory.write(code)
        call = ctypes.CFUNCTYPE(None, ctypes.c_uint32, ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32))(
            ctypes.addressof(ctypes.c_char.from_buffer(memory))
        )
        first, seventh = (ctypes.c_uint32 * 4)(), (ctypes.c_uint32 * 4)()
        call(1, 0, first)
        call(7, 0, seventh)
        result = {name: bool(first[2] & (1 << bit)) for name, bit in {
            "sse3": 0, "ssse3": 9, "sse4_1": 19, "sse4_2": 20, "popcnt": 23,
            "avx": 28, "fma": 12, "f16c": 29,
        }.items()}
        result["avx2"] = bool(seventh[1] & (1 << 5))
        return result


class EmbeddingFixture(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_POST(self):
        if self.path != "/v1/embeddings":
            self.send_error(404)
            return
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        inputs = body["input"]
        if not isinstance(inputs, list):
            inputs = [inputs]
        payload = {
            "object": "list", "model": "cpu-test",
            "data": [
                {"object": "embedding", "index": index, "embedding": [1.0] + [0.0] * 31}
                for index in range(len(inputs))
            ],
            "usage": {"prompt_tokens": 1, "total_tokens": 1},
        }
        encoded = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


async def exercise(args, endpoint: str) -> None:
    import lancedb

    from sag_api.core.config import Settings
    from sag_api.db.models import Source
    from sag_api.sag.engine_manager import EngineManager
    from zleap.sag.pipeline import SourceDescriptor

    assert version("zleap-sag") == "0.15.0"
    assert lancedb.__version__ == args.expected_version
    print(json.dumps({"sdk": version("zleap-sag"), "lancedb": lancedb.__version__}), flush=True)
    settings = Settings(
        _env_file=None, data_dir=str(args.data_dir),
        llm_api_key="cpu-test", llm_model="openai/cpu-test",
        embedding_api_key="cpu-test", embedding_model="cpu-test",
        embedding_dimensions=32, embedding_base_url=endpoint,
    )
    manager = EngineManager(settings)
    source = Source(
        id="cpu-source", name="CPU compatibility", sag_source_config_id="cpu-datasource", config={},
    )
    try:
        slot = await manager._slot("cpu-datasource", source)
        print("SDK_INIT_OK", flush=True)
        if args.mode == "write":
            descriptor = SourceDescriptor(
                data_source_id="cpu-datasource", source_id="cpu-document", source_type="text", title="CPU test",
            )
            chunks = await slot.engine.chunk_text(
                "SAG compatibility document about the blue ocean project.", descriptor,
            )
            indexed = await slot.engine.index(chunks, descriptor)
            assert indexed.relation_status == "succeeded"
            assert indexed.vector_status == "succeeded"
            assert indexed.chunk_ids
            print("SDK_DOCUMENT_INDEX_OK", flush=True)
        # Use the same batch-vector path as the fnOS application, including
        # embedding generation, data_source_id filters, and chunk hydration.
        result = await manager._search._search_chunk_vectors(
            [("cpu-datasource", source)], "blue ocean", top_k=3, requested_sources=1,
        )
        assert result.sections, "No document was retrieved"
        assert any("blue ocean" in item.content for item in result.sections)
        assert any(item.source_id == "cpu-document" for item in result.sections)
        print(json.dumps({"retrieved": len(result.sections), "mode": args.mode}), flush=True)
    finally:
        await manager.aclose_all()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--mode", choices=["write", "reopen"], required=True)
    parser.add_argument("--expected-version", default="0.41.0")
    parser.add_argument("--require-no-avx", action="store_true")
    args = parser.parse_args()
    if args.mode == "write" and args.data_dir.exists():
        parser.error("write requires a new isolated data directory")
    if args.mode == "reopen" and not (args.data_dir / "sag.db").is_file():
        parser.error("reopen requires the previously created engine database")
    features = cpu_features()
    print(json.dumps({"cpuid": features}, sort_keys=True), flush=True)
    if args.require_no_avx:
        assert not any(features[name] for name in ("avx", "avx2", "fma", "f16c")), features
    with ThreadingHTTPServer(("127.0.0.1", 0), EmbeddingFixture) as server:
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            asyncio.run(exercise(args, f"http://127.0.0.1:{server.server_port}/v1"))
        finally:
            server.shutdown()
    print("CPU_COMPATIBILITY_PASS", flush=True)


if __name__ == "__main__":
    main()
