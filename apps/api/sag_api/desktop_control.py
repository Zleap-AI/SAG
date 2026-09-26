"""Private desktop lifecycle control; never installed by the Web API entry point."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from ipaddress import ip_address
from secrets import compare_digest

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send


class DesktopControl:
    def __init__(
        self, app: ASGIApp, token: str, activity: Callable[[], Awaitable[bool]], shutdown: Callable[[], None]
    ) -> None:
        self.app = app
        self.token = token
        self.activity = activity
        self.shutdown = shutdown
        self.active_requests = 0

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        if scope["type"] == "http" and path.startswith("/_desktop/"):
            request = Request(scope, receive)
            try:
                local = request.client is not None and ip_address(request.client.host).is_loopback
            except ValueError:
                local = False
            supplied = request.headers.get("x-sag-desktop-token", "")
            if not local or not compare_digest(supplied.encode(), self.token.encode()):
                await JSONResponse({"error": "Forbidden"}, status_code=403)(scope, receive, send)
                return
            if request.method != "POST" or path not in {"/_desktop/activity", "/_desktop/shutdown"}:
                await JSONResponse({"error": "Not found"}, status_code=404)(scope, receive, send)
                return
            if path == "/_desktop/shutdown":
                await JSONResponse({"stopping": True})(scope, receive, send)
                self.shutdown()
                return
            try:
                active = self.active_requests > 0 or await self.activity()
            except Exception:  # Inspection failure must never be mistaken for idle.
                await JSONResponse({"error": "Activity unavailable"}, status_code=503)(scope, receive, send)
                return
            await JSONResponse({"active": active})(scope, receive, send)
            return

        tracked = scope["type"] in {"http", "websocket"} and path not in {
            "/api/v1/system/health",
            "/api/v1/system/ready",
        }
        if tracked:
            self.active_requests += 1
        try:
            await self.app(scope, receive, send)
        finally:
            if tracked:
                self.active_requests -= 1


async def has_background_work(app) -> bool:
    """Include queued jobs and transfers, not just currently executing workers."""
    from sqlalchemy import select

    from sag_api.core.db import SessionLocal
    from sag_api.db.models import Document, Job
    from sag_api.db.models.octx import OctxTransfer
    from sag_api.enums import DocumentStatus, JobStatus, OctxTransferStatus

    bootstrap = getattr(app.state, "storage_bootstrap", None)
    if bootstrap is not None and bootstrap.public_status().get("phase") == "processing":
        return True
    async with SessionLocal() as session:
        job = await session.scalar(select(Job.id).where(Job.status.in_([JobStatus.QUEUED, JobStatus.RUNNING])).limit(1))
        if job is not None:
            return True
        # A paused job can still be draining its in-flight document chunks.
        pausing = await session.scalar(select(Document.id).where(Document.status == DocumentStatus.PAUSING).limit(1))
        if pausing is not None:
            return True
        transfer = await session.scalar(
            select(OctxTransfer.id)
            .where(
                OctxTransfer.status.in_(
                    [
                        OctxTransferStatus.VALIDATING,
                        OctxTransferStatus.QUEUED,
                        OctxTransferStatus.IMPORTING,
                        OctxTransferStatus.INDEXING,
                        OctxTransferStatus.SWITCHING,
                        OctxTransferStatus.EXPORTING,
                        OctxTransferStatus.PACKAGING,
                    ]
                )
            )
            .limit(1)
        )
        return transfer is not None
