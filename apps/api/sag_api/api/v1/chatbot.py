"""Authenticated administrative connections. Draft tests never persist credentials."""

from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from sqlalchemy.ext.asyncio import AsyncSession

from sag_api.core.chatbot_config import EmbeddingUpdate, LLMUpdate, OriginalEmbeddingDraft, TestDraft, Update
from sag_api.core.config import expand_embedding_dimensions
from sag_api.core.db import get_session
from sag_api.core.deps import get_current_user
from sag_api.core.errors import ConfigurationError
from sag_api.generation.chatbot import QueryLLM
from sag_api.sag.embedding_probe import probe_embedding_connection
from sag_api.schemas.system import ModelConfigUpdate
from sag_api.services import chatbot_service as rt

_SAFE_DRAFT_PATHS = {
    **{
        ("body", name): name
        for schema in (OriginalEmbeddingDraft, ModelConfigUpdate, TestDraft)
        for name in schema.model_fields
    },
    **{("body", "llm", name): "llm." + name for name in LLMUpdate.model_fields},
    **{("body", "embedding", name): "embedding." + name for name in EmbeddingUpdate.model_fields},
}
_SAFE_VALIDATION_MESSAGES = {
    "missing": "is required",
    "string_type": "must be text",
    "string_too_short": "must not be empty",
    "string_too_long": "is too long",
    "int_type": "must be an integer",
    "int_parsing": "must be an integer",
    "int_from_float": "must be an integer",
    "float_type": "must be a number",
    "float_parsing": "must be a number",
    "bool_type": "must be true or false",
    "bool_parsing": "must be true or false",
    "greater_than_equal": "is outside the allowed range",
    "less_than_equal": "is outside the allowed range",
    "literal_error": "has an unsupported value",
    "extra_forbidden": "has an unexpected field",
    "json_invalid": "must contain valid JSON",
}


class SecretSafeRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()

        async def handle(request):
            try:
                return await original(request)
            except RequestValidationError as exc:
                fields = []
                for failure in exc.errors():
                    # Never echo raw Pydantic messages, inputs, context or user-supplied key names.
                    item = {
                        "field": _SAFE_DRAFT_PATHS.get(tuple(failure.get("loc", ())), "request"),
                        "message": _SAFE_VALIDATION_MESSAGES.get(failure.get("type"), "has an invalid value"),
                    }
                    if item not in fields:
                        fields.append(item)
                summary = "; ".join(f"{item['field']} ({item['message']})" for item in fields)
                return JSONResponse(
                    status_code=422,
                    content={
                        "error": {
                            "message": "Invalid connection draft: " + summary,
                            "code": "VALIDATION_ERROR",
                            "fields": fields,
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
    updates = expand_embedding_dimensions({
        key: (None if key.endswith("base_url") and value == "" else value)
        for key, value in body.model_dump(exclude_unset=True).items()
        if not (key.endswith("api_key") and not value)
    })
    stock = rt.manager.stock
    active = stock.model_copy(update=updates, deep=True)
    if (
        active.llm_provider != stock.llm_provider
        and "responses" in {active.llm_provider, stock.llm_provider}
        and not updates.get("llm_api_key")
    ):
        active.llm_api_key = None
    return await probe_embedding_connection(active)


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
