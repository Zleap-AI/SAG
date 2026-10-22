"""Final ASGI client boundary and durable write result recovery.

The authority callback validates the deployment identity and returns a tenant
SessionStore. The original application retains all source/job ownership checks.
"""

from __future__ import annotations

import hashlib
import json
from urllib.parse import parse_qsl, urlencode

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from sag_api.client_access.allowlist import business_allowed, is_write
from sag_api.client_access.core import ClientAccessError


class ClientBusinessMiddleware:
    def __init__(self, app, authority, max_body_bytes=256 * 1024 * 1024):
        self.app, self.authority, self.max_body_bytes = app, authority, max_body_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope["path"].startswith("/client/v1/api"):
            await self.app(scope, receive, send)
            return
        path = scope["path"].removeprefix("/client/v1/api")
        try:
            if not business_allowed(scope["method"], path, scope.get("raw_path")):
                raise ClientAccessError("route_not_allowed", 404)
            request = Request(scope, receive)
            store, claims = await self.authority(request)
            write = is_write(scope["method"], path)
            body = bytearray()
            if write:
                async for chunk in request.stream():
                    body.extend(chunk)
                    if len(body) > self.max_body_bytes:
                        raise ClientAccessError("request_too_large", 413)
                data = bytes(body)

                async def replay_body():
                    return {"type": "http.request", "body": data, "more_body": False}

                replay = Request(scope, replay_body)
                fingerprint = data
                if request.headers.get("content-type", "").startswith("multipart/form-data"):
                    form = await replay.form()
                    canonical = []
                    for name, value in form.multi_items():
                        if hasattr(value, "read"):
                            content = await value.read()
                            canonical.append(
                                [name, value.filename, value.content_type, hashlib.sha256(content).hexdigest()]
                            )
                        else:
                            canonical.append([name, value])
                    fingerprint = json.dumps(canonical, sort_keys=True).encode()
                    await form.close()
                elif request.headers.get("content-type", "").startswith("application/json"):
                    try:
                        fingerprint = json.dumps(json.loads(data), sort_keys=True, separators=(",", ":")).encode()
                    except ValueError:
                        pass
                key = request.headers.get("idempotency-key", "")
                query = urlencode(sorted(parse_qsl(scope.get("query_string", b"").decode())))
                operation = store.begin_operation(
                    claims["subject"], key, scope["method"], path + "?" + query, fingerprint
                )
                if not operation["created"]:
                    if operation["status"] == "pending":
                        await JSONResponse(operation, status_code=202, headers={"Retry-After": "2"})(
                            scope, receive, send
                        )
                    else:
                        response = (
                            Response(status_code=204, headers={"X-Operation-Id": operation["operation_id"]})
                            if operation["http_status"] == 204
                            else JSONResponse(operation["response"], status_code=operation["http_status"])
                        )
                        await response(scope, receive, send)
                    return
                sent_body = False

                async def downstream_receive():
                    nonlocal sent_body
                    if not sent_body:
                        sent_body = True
                        return {"type": "http.request", "body": data, "more_body": False}
                    return await receive()
            else:
                downstream_receive = receive
            inner = dict(scope)
            inner["path"] = "/api/v1" + path
            inner["raw_path"] = inner["path"].encode()
            inner.setdefault("state", {})["client_access"] = claims
            if not write:
                await self.app(inner, downstream_receive, send)
                return
            messages = []

            async def capture(message):
                messages.append(message)

            # Record before forwarding any response. A crash leaves a durable pending
            # operation; it is never automatically repeated with unknown side effects.
            await self.app(inner, downstream_receive, capture)
            start = next(m for m in messages if m["type"] == "http.response.start")
            raw = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
            try:
                result = json.loads(raw)
            except ValueError:
                result = {"detail": "operation response unavailable"}
            if isinstance(result, dict):
                result["operation_id"] = operation["operation_id"]
                if "source_id" in result and "filename" in result:
                    result["document_id"] = result.get("id")
                if "type" in result and "attempts" in result:
                    result["job_id"] = result.get("id")
            store.complete_operation(claims["subject"], key, start["status"], result)
            if start["status"] == 204:
                await Response(status_code=204, headers={"X-Operation-Id": operation["operation_id"]})(
                    scope, receive, send
                )
                return
            await JSONResponse(
                result,
                status_code=start["status"],
                headers={
                    k.decode(): v.decode()
                    for k, v in start["headers"]
                    if k.lower() not in {b"content-length", b"content-type"}
                },
            )(scope, receive, send)
        except ClientAccessError as error:
            await JSONResponse({"error": error.code}, status_code=error.status)(scope, receive, send)
