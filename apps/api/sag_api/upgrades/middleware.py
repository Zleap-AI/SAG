from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

ALLOWED_PREFIXES = (
    "/api/v1/system/health",
    "/api/v1/system/ready",
    "/api/v1/system/storage-bootstrap",
    "/api/v1/auth/login",
    "/api/v1/auth/me",
)


class StorageMaintenanceMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        coordinator = getattr(request.app.state, "storage_bootstrap", None)
        path = request.url.path
        allowed = path in ALLOWED_PREFIXES or path.startswith("/api/v1/system/storage-bootstrap/")
        if coordinator is not None and not coordinator.runtime_ready() and not allowed:
            return JSONResponse(
                status_code=503,
                content={
                    "error": {
                        "code": "storage_upgrade_required",
                        "message": "storage upgrade choice required",
                    }
                },
            )
        return await call_next(request)
