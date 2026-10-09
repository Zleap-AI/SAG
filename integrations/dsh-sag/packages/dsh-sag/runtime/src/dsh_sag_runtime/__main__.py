"""stdio entrypoint for the managed dsh-sag runtime."""

from __future__ import annotations

import argparse
import asyncio
import importlib.metadata
import sys
from typing import BinaryIO, TextIO

from zleap.sag import EngineConfig

from .engines import EnginePool
from .evidence import EvidenceRefCodec
from .protocol import REQUIRED_ENGINE_VERSION, decode_request, encode_response
from .read import ReadAdapter
from .search import SearchAdapter
from .server import RuntimeServer


async def run_stdio(server: RuntimeServer, reader: BinaryIO, writer: BinaryIO, diagnostics: TextIO) -> None:
    """Serve requests until stdin closes, keeping stdout protocol-only."""
    write_lock = asyncio.Lock()
    tasks: set[asyncio.Task[None]] = set()

    async def process(frame: bytes) -> None:
        try:
            request = decode_request(frame.rstrip(b"\r\n"))
            response = await server.accept(request)
            if response is not None:
                encoded = encode_response(response)
                async with write_lock:
                    writer.write(encoded)
                    writer.flush()
        except Exception as error:
            print(f"dsh-sag-runtime rejected a frame: {type(error).__name__}", file=diagnostics, flush=True)

    try:
        while frame := await asyncio.to_thread(reader.readline):
            task = asyncio.create_task(process(frame))
            tasks.add(task)
            task.add_done_callback(tasks.discard)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        await server.aclose()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="dsh-sag-runtime")
    parser.add_argument("--env-file", required=True)
    parser.add_argument("--namespace", action="append", required=True)
    parser.add_argument("--max-read-engines", type=int, default=4)
    parser.add_argument("--max-results", type=int, default=20)
    parser.add_argument("--max-excerpt-chars", type=int, default=1200)
    parser.add_argument("--max-read-chars", type=int, default=40_000)
    return parser.parse_args(argv)


async def async_main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    engine_version = importlib.metadata.version("zleap-sag")
    if engine_version != REQUIRED_ENGINE_VERSION:
        raise RuntimeError(f"zleap-sag {REQUIRED_ENGINE_VERSION} is required")
    namespaces = tuple(dict.fromkeys(args.namespace))
    config = EngineConfig.from_env(args.env_file)
    pool = EnginePool(config, set(namespaces), max_read_engines=args.max_read_engines)
    codec = EvidenceRefCodec(set(namespaces))
    search = SearchAdapter(pool, codec, set(namespaces), max_results=args.max_results, max_excerpt_chars=args.max_excerpt_chars)
    read = ReadAdapter(pool, codec, max_read_chars=args.max_read_chars, max_events=20)
    server = RuntimeServer(pool, search, read, namespaces, engine_version=engine_version)
    await run_stdio(server, sys.stdin.buffer, sys.stdout.buffer, sys.stderr)


def main() -> None:
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
