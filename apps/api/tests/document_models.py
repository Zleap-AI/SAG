"""Deterministic local HTTP models; actual SDK, parser and stores remain real."""

from __future__ import annotations

import hashlib
import json
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class LocalModels:
    def __init__(self):
        self.calls: list[str] = []
        self.requests: list[dict] = []
        self.zero_calls: list[str] = []
        self.lock = threading.Lock()
        self.block_at: int | None = None
        self.blocked = threading.Event()
        self.release = threading.Event()
        self.base_url = ""

    def respond(self, path, body):
        if path.endswith("/embeddings"):
            inputs = body["input"]
            inputs = [inputs] if isinstance(inputs, str) else inputs
            return {
                "object": "list",
                "model": body["model"],
                "data": [
                    {
                        "object": "embedding",
                        "index": i,
                        "embedding": [0.1 + b / 255 for b in hashlib.sha256(str(t).encode()).digest()],
                    }
                    for i, t in enumerate(inputs)
                ],
                "usage": {"prompt_tokens": 1, "total_tokens": 1},
            }
        if not path.endswith("/chat/completions"):
            raise ValueError(path)
        signature = hashlib.sha256(json.dumps(body["messages"], sort_keys=True).encode()).hexdigest()
        with self.lock:
            self.calls.append(signature)
            self.requests.append(body)
            number = len(self.calls)
        if number == self.block_at:
            self.blocked.set()
            self.release.wait(timeout=90)
        time.sleep(0.04)
        result = {
            "type": "response",
            "data": {
                "items": [
                    {
                        "reason": "The document states a reusable knowledge fact.",
                        "title": f"Acme project {signature[:8]}",
                        "summary": "Acme completed the migration.",
                        "content": "Acme Corporation completed the database migration.",
                        "references": [1],
                        "entities": [
                            {
                                "type": "organization",
                                "name": "Acme Corporation",
                                "description": "Company",
                            }
                        ],
                        "is_valid": True,
                    }
                ]
            },
        }
        if "ZERO_ONLY" in json.dumps(body["messages"]):
            result["data"]["items"] = []
            self.zero_calls.append(signature)
        return {
            "id": f"chat-{signature[:8]}",
            "object": "chat.completion",
            "created": 1,
            "model": body["model"],
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": json.dumps(result)},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
        }


@contextmanager
def local_models():
    models = LocalModels()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            response = json.dumps(models.respond(self.path, body)).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            try:
                self.wfile.write(response)
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    models.base_url = f"http://127.0.0.1:{server.server_port}/v1"
    try:
        yield models
    finally:
        models.release.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
