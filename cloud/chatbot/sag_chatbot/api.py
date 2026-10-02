"""Authenticated administrative connections. Draft tests never persist credentials."""
from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from sag_api.core.db import get_session
from sag_api.core.deps import get_current_user
from sag_api.core.errors import ConfigurationError
from sqlalchemy.ext.asyncio import AsyncSession

from . import runtime as rt
from .config import TestDraft, Update
from .provider import QueryLLM


class SecretSafeRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()

        async def handle(request):
            try:
                return await original(request)
            except RequestValidationError:
                return JSONResponse(status_code=422, content={"error": {
                    "message": "Invalid connection draft; check field names and value types",
                    "code": "VALIDATION_ERROR",
                }})
        return handle


router = APIRouter(tags=["system"], route_class=SecretSafeRoute)


@router.get("/chatbot-config")
async def get_config(_user: Annotated[object, Depends(get_current_user)]):
    return {"config": rt.manager.public()}


@router.put("/chatbot-config")
async def save_config(body: Update, _user: Annotated[object, Depends(get_current_user)], session: Annotated[AsyncSession, Depends(get_session)]):
    return {"config": await rt.manager.save(session, body.model_dump(exclude_unset=True))}


@router.post("/chatbot-config/test")
async def test_config(body: TestDraft, _user: Annotated[object, Depends(get_current_user)]):
    if body.target not in {"llm", "embedding"}:
        raise ConfigurationError("Chatbot test target must be llm or embedding")
    patch = body.model_dump(exclude_unset=True, exclude={"target"})
    if patch:
        async with rt.manager.lock:
            _, snapshot = rt.manager.draft(patch, persist=False)
    else:
        snapshot = rt.manager.snapshot()
    if not snapshot.connections[body.target].enabled:
        raise ConfigurationError("Enable the separate connection in the test draft")
    async with rt.manager.scope(query=True, snapshot=snapshot):
        try:
            if body.target == "llm":
                await QueryLLM(snapshot.settings).complete([{"role": "user", "content": "ping"}])
            else:
                adapter = rt.ScopedAdapter(snapshot.adapter("embedding"), "embedding")
                await adapter.generate("ping")
        except Exception:  # noqa: BLE001 -- this boundary must hide every provider's error bodies.
            # Provider errors, including headers/bodies/URLs, never reach the administrative UI.
            return {"ok": False, "message": "Connection test failed; check endpoint, provider, model, credentials and server availability"}
    return {"ok": True, "message": "Connection successful"}
