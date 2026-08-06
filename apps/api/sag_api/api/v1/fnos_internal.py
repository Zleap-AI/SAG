"""Private worker-only fnOS endpoints used by the local supervisor."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from sag_api.core.config import settings
from sag_api.core.db import get_session
from sag_api.core.deps import get_current_user
from sag_api.core.errors import NotFoundError
from sag_api.db.models import Job, User
from sag_api.enums import JobStatus

router = APIRouter(prefix="/fnos-internal", tags=["fnos-internal"])


async def _require_fnos_mode() -> None:
    if settings.auth_mode != "fnos":
        raise NotFoundError()


@router.get("/worker-status", dependencies=[Depends(_require_fnos_mode)])
async def worker_status(
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, int]:
    """Return only the queued/running work counts for this UID's worker."""
    queued, running = (
        await session.execute(
            select(
                func.coalesce(func.sum(case((Job.status == JobStatus.QUEUED, 1), else_=0)), 0),
                func.coalesce(func.sum(case((Job.status == JobStatus.RUNNING, 1), else_=0)), 0),
            ).where(Job.status.in_([JobStatus.QUEUED, JobStatus.RUNNING]))
        )
    ).one()
    queued_count = int(queued)
    running_count = int(running)
    return {
        "queued": queued_count,
        "running": running_count,
        "active": queued_count + running_count,
    }
