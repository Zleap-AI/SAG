from fastapi import APIRouter, Depends, Request, status
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy.ext.asyncio import AsyncSession

from sag_api.core.db import get_session
from sag_api.core.deps import _bearer, get_current_user
from sag_api.core.errors import ConflictError
from sag_api.db.models import User
from sag_api.schemas.storage_bootstrap import StorageChoiceRequest
from sag_api.upgrades.types import StorageUpgradeError

router = APIRouter(prefix="/system/storage-bootstrap", tags=["system"])


async def _optional_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    session: AsyncSession = Depends(get_session),
) -> User | None:
    if credentials is None:
        return None
    return await get_current_user(request, credentials, session)


@router.get("")
async def get_status(request: Request, user: User | None = Depends(_optional_user)) -> dict:
    return request.app.state.storage_bootstrap.public_status(authenticated=user is not None)


@router.post("/choice", status_code=status.HTTP_202_ACCEPTED)
async def choose(
    request: Request,
    body: StorageChoiceRequest,
    user: User = Depends(get_current_user),
) -> dict:
    try:
        await request.app.state.storage_bootstrap.choose(body.choice, user.id)
    except StorageUpgradeError as error:
        raise ConflictError(str(error)) from error
    return request.app.state.storage_bootstrap.public_status(authenticated=True)
