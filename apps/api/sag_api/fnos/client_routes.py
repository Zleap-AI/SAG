"""Explicit client and authenticated Web management routes on the UDS gateway."""

from __future__ import annotations

import json
import time
from uuid import uuid4

import httpx
from fastapi import Request
from fastapi.responses import JSONResponse, Response

from sag_api.client_access.allowlist import client_allowed
from sag_api.client_access.core import ClientAccessError
from sag_api.core.errors import AuthError
from sag_api.fnos.identity import parse_gateway_identity
from sag_api.fnos.proxy import ProxyStreamingResponse, bearer_token, filtered_request_headers, filtered_response_headers
from sag_api.fnos.supervisor import WorkerCapacityError, WorkerStartError


async def _json(request):
    if not request.headers.get("content-type", "").split(";", 1)[0].strip().lower() == "application/json":
        raise ClientAccessError("invalid_request", 415)
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > 8192:
            raise ClientAccessError("invalid_request", 413)
    try:
        value = json.loads(data)
    except ValueError:
        raise ClientAccessError("invalid_request") from None
    if not isinstance(value, dict):
        raise ClientAccessError("invalid_request")
    return value


def install_client_routes(app, access, supervisor, signer, prefix):
    async def handle(request: Request, subpath: str = ""):
        path = request.url.path.removeprefix(prefix)
        try:
            if not client_allowed(request.method, path, request.scope.get("raw_path")):
                return JSONResponse({"detail": "not found"}, status_code=404)
            policy = access.policy()
            if path == "/client/v1/info":
                return JSONResponse(
                    {
                        "protocol": "client-v1",
                        "deployment": "fnos",
                        "server_id": policy["server_id"],
                        "api_base_path": "/client/v1/api",
                        "auth_methods": ["device_code"],
                        "verification_uri": policy["verification_uri"],
                    }
                )
            if path == "/client/v1/device/code":
                if not policy["enabled"]:
                    raise ClientAccessError("client_access_disabled", 403)
                body = await _json(request)
                if body.get("code_challenge_method") != "S256":
                    raise ClientAccessError("invalid_request")
                return JSONResponse(access.pairings.create(body.get("client_name"), body.get("code_challenge")))
            if path == "/client/v1/token":
                if not policy["enabled"]:
                    raise ClientAccessError("client_access_disabled", 403)
                body = await _json(request)
                grant = body.get("grant_type")
                key = request.headers.get("idempotency-key", "")
                if grant == "urn:ietf:params:oauth:grant-type:device_code":
                    return JSONResponse(access.pairings.poll(str(body.get("device_code", ""))))
                if grant == "urn:sag:params:oauth:grant-type:device-ticket":
                    return JSONResponse(
                        await access.exchange(
                            str(body.get("exchange_ticket", "")), str(body.get("code_verifier", "")), key
                        )
                    )
                if grant == "refresh_token":
                    token = str(body.get("refresh_token", ""))
                    # Signature/purpose/audience establish only the tenant lookup.
                    # Expired credentials can recover an existing exact response;
                    # they never authorize a new rotation or a business request.
                    from sag_api.fnos.identity import GatewayIdentity

                    routing = access.codec().decode(token, purpose="refresh", allow_expired=True)
                    route = routing["route"]
                    identity = GatewayIdentity(route["uid"], route["username"], False)
                    store = access.store(identity)
                    recovered = store.recover_rotation(token, key)
                    await access.route(recovered["refresh_token"] if recovered else token, "refresh")
                    return JSONResponse(recovered or store.rotate(token, key))
                raise ClientAccessError("unsupported_grant_type")
            token = bearer_token(request.scope["headers"])
            if token is None:
                raise ClientAccessError("invalid_grant", 401)
            if path == "/client/v1/session/revoke":
                try:
                    claims = access.codec().decode(token, purpose="access")
                except ClientAccessError:
                    claims = access.codec().decode(token, purpose="refresh")
                from sag_api.fnos.identity import GatewayIdentity

                route = claims["route"]
                identity = GatewayIdentity(route["uid"], route["username"], False)
                access.store(identity).revoke(token)
                return Response(status_code=204)
            if not policy["enabled"]:
                raise ClientAccessError("client_access_disabled", 403)
            identity = await access.route(token, "access")
            store = access.store(identity)
            claims = store.authenticate(token)
            if path == "/client/v1/session":
                row = next(s for s in store.list_sessions(claims["subject"]) if s["session_id"] == claims["session_id"])
                return JSONResponse(
                    {**row, "user": {"id": claims["subject"], "username": identity.username, "name": identity.username}}
                )
            if path.startswith("/client/v1/operations/"):
                result = store.operation(claims["subject"], path.rsplit("/", 1)[1])
                return JSONResponse(result or {"detail": "not found"}, status_code=200 if result else 404)
            try:
                lease = await supervisor.acquire(identity)
            except WorkerCapacityError as error:
                return JSONResponse(
                    {"error": "temporarily_unavailable"},
                    status_code=error.status_code,
                    headers={"Retry-After": str(error.retry_after)},
                )
            except WorkerStartError:
                return JSONResponse({"error": "temporarily_unavailable"}, status_code=503, headers={"Retry-After": "2"})

            async def release():
                if hasattr(lease, "release"):
                    await lease.release()
                else:
                    await lease.__aexit__(None, None, None)

            try:
                request_id = str(uuid4())
                signed = signer.sign(identity, request_id, int(time.time()), principal="client")
                headers = filtered_request_headers(request.scope["headers"])
                headers += [(k.lower().encode(), v.encode()) for k, v in signed.items()]
                headers += [(b"authorization", f"Bearer {token}".encode()), (b"x-request-id", request_id.encode())]
                upstream = lease.handle.client.build_request(
                    request.method,
                    path,
                    params=request.query_params,
                    headers=headers,
                    content=request.stream(),
                    timeout=httpx.Timeout(connect=5.0, read=None, write=30.0, pool=5.0),
                )
                response = await lease.handle.client.send(upstream, stream=True)
            except BaseException:
                await release()
                raise
            return ProxyStreamingResponse(
                response,
                release=release,
                headers={
                    k.decode("latin1"): v.decode("latin1") for k, v in filtered_response_headers(response.headers.raw)
                },
            )
        except ClientAccessError as error:
            return JSONResponse({"error": error.code}, status_code=error.status)

    async def web(request: Request, subpath: str = ""):
        try:
            identity = parse_gateway_identity(request.headers)
            subject = f"fnos_{identity.uid}"
            path = request.url.path.removeprefix(prefix + "/api/v1/client-access")
            if request.method == "GET" and path == "/info":
                return JSONResponse(access.metadata(identity))
            if request.method == "PUT" and path == "/policy":
                return JSONResponse(access.update_policy(identity, await _json(request)))
            if request.method == "POST" and path == "/inspect":
                body = await _json(request)
                code = str(body.get("user_code", "")).upper().replace("-", "")
                with access.pairings.lock:
                    access.pairings._prune(time.time())
                    entry = next((v for v in access.pairings.entries.values() if v["user_code"] == code), None)
                    if entry is None or entry["state"] != "pending":
                        raise ClientAccessError("expired_token")
                    return JSONResponse(
                        {
                            "client_name": entry["client_name"],
                            "user_name": identity.username,
                            "scope": "knowledge:read knowledge:write",
                        }
                    )
            if request.method == "POST" and path == "/approve":
                body = await _json(request)
                if not isinstance(body.get("user_code"), str) or type(body.get("approve")) is not bool:
                    raise ClientAccessError("invalid_request")
                await access.approve(identity, body["user_code"], body["approve"])
                return Response(status_code=204)
            if request.method == "GET" and path == "/sessions":
                return JSONResponse(access.store(identity).list_sessions(subject))
            if request.method == "DELETE" and path.startswith("/sessions/"):
                changed = access.store(identity).revoke_session(subject, path.rsplit("/", 1)[1])
                return Response(status_code=204 if changed else 404)
            return JSONResponse({"detail": "not found"}, status_code=404)
        except AuthError:
            return JSONResponse({"error": "web_authentication_required"}, status_code=401)
        except ClientAccessError as error:
            return JSONResponse({"error": error.code}, status_code=error.status)

    app.add_api_route(
        prefix + "/client/v1/{subpath:path}",
        handle,
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
    )
    app.add_api_route(prefix + "/api/v1/client-access/{subpath:path}", web, methods=["GET", "POST", "PUT", "DELETE"])
