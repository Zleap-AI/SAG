from fastapi import APIRouter

from sag_api.api.v1 import (
    activity,
    agents,
    attachments,
    auth,
    documents,
    fnos_internal,
    insights,
    jobs,
    knowledge,
    openai,
    search,
    sources,
    system,
    universe,
)

api_router = APIRouter(prefix="/api/v1")
for _module in (
    auth,
    sources,
    documents,
    fnos_internal,
    insights,
    knowledge,
    jobs,
    search,
    agents,
    openai,
    activity,
    attachments,
    system,
    universe,
):
    api_router.include_router(_module.router)
api_router.include_router(search.global_router)

__all__ = ["api_router"]
