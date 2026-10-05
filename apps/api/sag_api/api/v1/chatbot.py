"""Authenticated administrative connections. Draft tests never persist credentials."""

import math
from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from sqlalchemy.ext.asyncio import AsyncSession

from sag_api.core.chatbot_config import OriginalEmbeddingDraft, TestDraft, Update
from sag_api.core.db import get_session
from sag_api.core.deps import get_current_user
from sag_api.core.errors import ConfigurationError
from sag_api.generation.chatbot import QueryLLM
from sag_api.services import chatbot_service as rt


class SecretSafeRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()

        async def handle(request):
            try:
                return await original(request)
            except RequestValidationError:
                return JSONResponse(
                    status_code=422,
                    content={
                        "error": {
                            "message": "Invalid connection draft; check field names and value types",
                            "code": "VALIDATION_ERROR",
                        }
                    },
                )

        return handle


router = APIRouter(tags=["system"], route_class=SecretSafeRoute)


@router.post("/model-config/embedding/test")
async def test_original_embedding(
    body: OriginalEmbeddingDraft,
    _user: Annotated[object, Depends(get_current_user)],
):
    from zleap.sag.config import EmbeddingConfig
    from zleap.sag.core.adapters.defaults import OpenAIEmbeddingAdapter

    updates = {
        key: (None if key.endswith("base_url") and value == "" else value)
        for key, value in body.model_dump(exclude_unset=True).items()
        if not (key.endswith("api_key") and not value)
    }
    stock = rt.manager.stock
    active = stock.model_copy(update=updates, deep=True)
    if (
        active.llm_provider != stock.llm_provider
        and "responses" in {active.llm_provider, stock.llm_provider}
        and not updates.get("llm_api_key")
    ):
        active.llm_api_key = None
    if not active.effective_embedding_api_key:
        return {"ok": False, "message": "No embedding API key configured"}
    try:
        # Use the original adapter directly; query connections cannot replace this test.
        adapter = OpenAIEmbeddingAdapter(
            config=EmbeddingConfig(
                model=active.embedding_model,
                api_key=active.effective_embedding_api_key,
                base_url=active.effective_embedding_base_url,
                schema_dimensions=active.effective_embedding_schema_dimensions,
                request_dimensions=active.effective_embedding_request_dimensions,
                timeout=active.embedding_timeout,
            )
        )
        try:
            vector = await adapter.generate("SAG embedding connection test")
            if not all(math.isfinite(value) for value in vector):
                raise ValueError("Non-finite embedding vector")
        finally:
            await adapter.close()
    except Exception:  # noqa: BLE001 -- provider bodies and credentials must stay private.
        return {
            "ok": False,
            "message": "Embedding test failed; check endpoint, model, credentials, dimensions and server availability",
        }
    return {
        "ok": True,
        "message": f"Embedding connection successful · {len(vector)} dimensions",
        "dimensions": len(vector),
    }


@router.get("/chatbot-config")
async def get_config(_user: Annotated[object, Depends(get_current_user)]):
    return {"config": rt.manager.public()}


@router.put("/chatbot-config")
async def save_config(
    body: Update,
    _user: Annotated[object, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
):
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
                message = f"连接成功 · {snapshot.settings.llm_provider} / {snapshot.settings.llm_model}"
            else:
                adapter = rt.ScopedAdapter(snapshot.adapter("embedding"), "embedding")
                vector = await adapter.generate("ping")
                message = f"Embedding connection successful · {len(vector)} dimensions"
        except Exception:  # noqa: BLE001 -- this boundary must hide every provider's error bodies.
            # Provider errors, including headers/bodies/URLs, never reach the administrative UI.
            return {
                "ok": False,
                "message": (
                    "Connection test failed; check endpoint, provider, model, credentials and server availability"
                ),
            }
    return {"ok": True, "message": message}
