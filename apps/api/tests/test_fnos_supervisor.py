import asyncio
import time
from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from sag_api.core.config import settings
from sag_api.core.db import get_session
from sag_api.core.errors import ApiError
from sag_api.db.models import Job, User
from sag_api.enums import JobStatus, JobType
from sag_api.fnos.identity import GatewayIdentity


@pytest.fixture(autouse=True)
def _pure_uid_isolation_by_default(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("SAG_FNOS_USERNAME_ISOLATION", raising=False)


class Clock:
    def __init__(self) -> None:
        self.value = 0.0

    def now(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class FakeProcess:
    def __init__(self, *, exits_after_term: bool = True) -> None:
        self.exits_after_term = exits_after_term
        self.returncode: int | None = None
        self.signals: list[str] = []
        self._done = asyncio.Event()

    def terminate(self) -> None:
        self.signals.append("TERM")
        if self.exits_after_term:
            self.returncode = 0
            self._done.set()

    def kill(self) -> None:
        self.signals.append("KILL")
        self.returncode = -9
        self._done.set()

    async def wait(self) -> int:
        if not self.exits_after_term and self.returncode is None:
            raise TimeoutError
        await self._done.wait()
        return self.returncode or 0


class FakeClient:
    def __init__(self, *, active: int = 0) -> None:
        self.active = active
        self.closed = False

    async def get(self, path: str, **_kwargs):
        if path == "/api/v1/system/ready":
            return FakeResponse(0)
        assert path == "/api/v1/fnos-internal/worker-status"
        return FakeResponse(self.active)

    async def aclose(self) -> None:
        self.closed = True


class FakeResponse:
    status_code = 200

    def __init__(self, active: int) -> None:
        self._active = active

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, int]:
        return {"queued": self._active, "running": 0, "active": self._active}


class SignedStatusClient(FakeClient):
    def __init__(self, signer, uid: int) -> None:
        super().__init__()
        self._signer = signer
        self._uid = uid

    async def get(self, path: str, **kwargs):
        if path == "/api/v1/fnos-internal/worker-status":
            self._signer.verify(kwargs.get("headers", {}), expected_uid=self._uid, now=int(time.time()))
        return await super().get(path, **kwargs)


class BlockingStopProcess(FakeProcess):
    def __init__(self) -> None:
        super().__init__(exits_after_term=False)
        self.termination_started = asyncio.Event()
        self.allow_exit = asyncio.Event()

    def terminate(self) -> None:
        self.signals.append("TERM")
        self.termination_started.set()

    async def wait(self) -> int:
        await self.allow_exit.wait()
        self.returncode = 0
        return 0


class BlockingReadyClient(FakeClient):
    def __init__(self) -> None:
        super().__init__()
        self.readiness_checked = asyncio.Event()

    async def get(self, path: str, **kwargs):
        if path == "/api/v1/system/ready":
            self.readiness_checked.set()
            raise RuntimeError("worker is not ready")
        return await super().get(path, **kwargs)


class BlockingStopFactory:
    def __init__(self) -> None:
        self.spawn_count = 0
        self.processes: dict[int, FakeProcess] = {}

    async def spawn(self, identity, _paths):
        self.spawn_count += 1
        process = BlockingStopProcess() if identity.uid == 1000 else FakeProcess()
        self.processes[identity.uid] = process
        return process, FakeClient()


class BlockingReadyFactory:
    def __init__(self) -> None:
        self.process = FakeProcess()
        self.client = BlockingReadyClient()

    async def spawn(self, _identity, _paths):
        return self.process, self.client


class VanishingProcess(FakeProcess):
    def terminate(self) -> None:
        raise ProcessLookupError


class VanishingWaitProcess(FakeProcess):
    def terminate(self) -> None:
        self.signals.append("TERM")

    async def wait(self) -> int:
        raise ProcessLookupError


class SingleProcessFactory:
    def __init__(self, process: FakeProcess) -> None:
        self.process = process

    async def spawn(self, _identity, _paths):
        return self.process, FakeClient()


@dataclass
class FakeFactory:
    starts: dict[int, Exception | None] | None = None
    exit_after_term: bool = True

    def __post_init__(self) -> None:
        self.spawn_count = 0
        self.processes: dict[int, FakeProcess] = {}
        self.clients: dict[int, FakeClient] = {}

    async def spawn(self, identity, paths):
        self.spawn_count += 1
        failure = (self.starts or {}).get(identity.uid)
        if failure is not None:
            paths.socket_file.touch()
            raise failure
        process = FakeProcess(exits_after_term=self.exit_after_term)
        client = FakeClient()
        self.processes[identity.uid] = process
        self.clients[identity.uid] = client
        return process, client


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def supervisor(tmp_path: Path, clock: Clock):
    from sag_api.fnos.supervisor import WorkerSupervisor

    factory = FakeFactory()
    return WorkerSupervisor(
        tmp_path / "data",
        tmp_path / "tmp",
        process_factory=factory,
        clock=clock.now,
    )


@pytest.mark.asyncio
async def test_concurrent_first_access_spawns_once(supervisor):
    """Removing the UID start lock would make this start one process per request."""
    leases = await asyncio.gather(
        *[supervisor.acquire(GatewayIdentity(1000, "Alice", False)) for _ in range(20)]
    )

    assert supervisor.process_factory.spawn_count == 1
    assert all(lease.handle.in_flight == 20 for lease in leases)

    for lease in leases:
        await lease.release()


@pytest.mark.asyncio
async def test_fifth_uid_is_rejected_at_worker_capacity(supervisor):
    """Increasing the registry capacity above four must fail this test."""
    from sag_api.fnos.supervisor import WorkerCapacityError

    for uid in range(1, 5):
        await (await supervisor.acquire(GatewayIdentity(uid, f"user-{uid}", False))).release()

    with pytest.raises(WorkerCapacityError) as error:
        await supervisor.acquire(GatewayIdentity(5, "user-5", False))

    assert error.value.status_code == 503
    assert error.value.retry_after == 5


@pytest.mark.asyncio
async def test_same_uid_with_different_usernames_gets_separate_workers_when_enabled(
    supervisor, monkeypatch: pytest.MonkeyPatch
):
    """Catches a reused Debian UID being routed into the previous owner's worker."""
    monkeypatch.setenv("SAG_FNOS_USERNAME_ISOLATION", "1")
    first = await supervisor.acquire(GatewayIdentity(1000, "Alice", False))
    second = await supervisor.acquire(GatewayIdentity(1000, "Bob", False))

    assert supervisor.process_factory.spawn_count == 2
    assert first.handle is not second.handle
    assert first.handle.socket_file != second.handle.socket_file
    assert first.handle.socket_file.name == "1000-Alice-3bc51062.sock"
    assert second.handle.socket_file.name == "1000-Bob-cd9fb1e1.sock"

    await first.release()
    await second.release()


@pytest.mark.asyncio
async def test_same_uid_shares_one_worker_by_default(supervisor):
    """Catches the switch-off default splitting one UID across multiple workers."""
    first = await supervisor.acquire(GatewayIdentity(1000, "Alice", False))
    second = await supervisor.acquire(GatewayIdentity(1000, "Bob", False))

    assert supervisor.process_factory.spawn_count == 1
    assert first.handle is second.handle
    assert first.handle.socket_file.name == "1000.sock"

    await first.release()
    await second.release()


@pytest.mark.asyncio
async def test_failed_start_removes_only_its_own_socket(tmp_path: Path, clock: Clock):
    """A failed UID must not clean another worker's socket."""
    from sag_api.fnos.supervisor import WorkerStartError, WorkerSupervisor

    factory = FakeFactory(starts={1001: RuntimeError("not ready")})
    supervisor = WorkerSupervisor(
        tmp_path / "data", tmp_path / "tmp", process_factory=factory, clock=clock.now
    )
    other_socket = tmp_path / "tmp/workers/1000.sock"
    other_socket.parent.mkdir(parents=True)
    other_socket.touch()

    with pytest.raises(WorkerStartError, match="spawn") as error:
        await supervisor.acquire(GatewayIdentity(1001, "Bob", False))

    assert isinstance(error.value.__cause__, RuntimeError)

    assert other_socket.exists()
    assert not (tmp_path / "tmp/workers/1001.sock").exists()


@pytest.mark.asyncio
async def test_reaper_keeps_busy_worker(supervisor, clock):
    """Dropping the in-flight guard would reap an active request."""
    lease = await supervisor.acquire(GatewayIdentity(1000, "Alice", False))
    clock.advance(901)

    assert await supervisor.reap_idle(clock.now()) == []

    await lease.release()


@pytest.mark.asyncio
async def test_reaper_keeps_streaming_worker(supervisor, clock):
    """Ignoring streams would cut off a live WebSocket or SSE connection."""
    lease = await supervisor.acquire(GatewayIdentity(1000, "Alice", False))
    await lease.release()
    lease.streams += 1
    clock.advance(901)

    assert await supervisor.reap_idle(clock.now()) == []

    lease.streams -= 1


@pytest.mark.asyncio
async def test_reaper_keeps_worker_with_active_job(supervisor, clock):
    """Skipping the worker-status query would kill queued or running jobs."""
    lease = await supervisor.acquire(GatewayIdentity(1000, "Alice", False))
    await lease.release()
    supervisor.process_factory.clients[1000].active = 1
    clock.advance(901)

    assert await supervisor.reap_idle(clock.now()) == []


@pytest.mark.asyncio
async def test_reaper_signs_the_worker_status_request(tmp_path: Path, clock: Clock, monkeypatch):
    """An unsigned reaper call is rejected by the same signer the worker endpoint uses."""
    from sag_api.fnos.identity import InternalIdentitySigner
    from sag_api.fnos.supervisor import WorkerSupervisor

    secret_path = tmp_path / "internal.key"
    secret_path.write_text("d" * 64, encoding="ascii")
    secret_path.chmod(0o600)
    monkeypatch.setitem(settings.__dict__, "fnos_internal_secret_file", str(secret_path))
    signer = InternalIdentitySigner.from_file(secret_path)

    factory = FakeFactory()
    signed_client = SignedStatusClient(signer, 1000)
    original_spawn = factory.spawn

    async def spawn_signed(identity, paths):
        process, _client = await original_spawn(identity, paths)
        return process, signed_client

    factory.spawn = spawn_signed
    supervisor = WorkerSupervisor(
        tmp_path / "data", tmp_path / "tmp", process_factory=factory, clock=clock.now
    )
    lease = await supervisor.acquire(GatewayIdentity(1000, "Alice", False))
    await lease.release()
    clock.advance(901)

    assert await supervisor.reap_idle(clock.now()) == ["1000"]


@pytest.mark.asyncio
async def test_reaper_keeps_capacity_reserved_until_process_exits(tmp_path: Path, clock: Clock):
    """Removing a handle before its blocked teardown must not free a fifth process slot."""
    from sag_api.fnos.supervisor import WorkerCapacityError, WorkerSupervisor

    factory = BlockingStopFactory()
    supervisor = WorkerSupervisor(
        tmp_path / "data", tmp_path / "tmp", process_factory=factory, clock=clock.now
    )
    for uid in range(1000, 1004):
        await (await supervisor.acquire(GatewayIdentity(uid, str(uid), False))).release()
    clock.advance(901)
    reaping = asyncio.create_task(supervisor.reap_idle(clock.now()))
    blocked = factory.processes[1000]
    await blocked.termination_started.wait()

    try:
        with pytest.raises(WorkerCapacityError):
            await supervisor.acquire(GatewayIdentity(1004, "fifth", False))
    finally:
        blocked.allow_exit.set()
        await reaping


@pytest.mark.asyncio
async def test_reaper_serializes_same_uid_replacement_until_process_exits(tmp_path: Path, clock: Clock):
    """Starting the same UID during teardown would rebind its live worker socket."""
    from sag_api.fnos.supervisor import WorkerSupervisor

    factory = BlockingStopFactory()
    supervisor = WorkerSupervisor(
        tmp_path / "data", tmp_path / "tmp", process_factory=factory, clock=clock.now
    )
    lease = await supervisor.acquire(GatewayIdentity(1000, "Alice", False))
    await lease.release()
    clock.advance(901)
    reaping = asyncio.create_task(supervisor.reap_idle(clock.now()))
    blocked = factory.processes[1000]
    await blocked.termination_started.wait()
    replacement = asyncio.create_task(supervisor.acquire(GatewayIdentity(1000, "Alice", False)))
    await asyncio.sleep(0)

    assert not replacement.done()

    blocked.allow_exit.set()
    await reaping
    lease = await replacement
    assert factory.spawn_count == 2
    await lease.release()


@pytest.mark.asyncio
async def test_close_waits_for_worker_that_has_spawned_but_is_not_ready(tmp_path: Path, clock: Clock):
    """Shutdown must not leave a child alive while its readiness probe is still retrying."""
    from sag_api.fnos.supervisor import WorkerStartError, WorkerSupervisor

    factory = BlockingReadyFactory()
    supervisor = WorkerSupervisor(
        tmp_path / "data", tmp_path / "tmp", process_factory=factory, clock=clock.now
    )
    acquiring = asyncio.create_task(supervisor.acquire(GatewayIdentity(1000, "Alice", False)))
    await factory.client.readiness_checked.wait()

    await asyncio.wait_for(supervisor.close(), timeout=1)

    assert factory.process.signals == ["TERM"]
    with pytest.raises(WorkerStartError, match="ready") as error:
        await acquiring
    assert isinstance(error.value.__cause__, RuntimeError)


@pytest.mark.asyncio
async def test_close_awaits_the_reaper_teardown_without_stopping_twice(tmp_path: Path, clock: Clock):
    """A simultaneous close must join the reaper's teardown instead of issuing a second TERM."""
    from sag_api.fnos.supervisor import WorkerSupervisor

    factory = BlockingStopFactory()
    supervisor = WorkerSupervisor(
        tmp_path / "data", tmp_path / "tmp", process_factory=factory, clock=clock.now
    )
    lease = await supervisor.acquire(GatewayIdentity(1000, "Alice", False))
    await lease.release()
    clock.advance(901)
    reaping = asyncio.create_task(supervisor.reap_idle(clock.now()))
    process = factory.processes[1000]
    await process.termination_started.wait()
    closing = asyncio.create_task(supervisor.close())
    await asyncio.sleep(0)

    assert process.signals == ["TERM"]
    assert not closing.done()

    process.allow_exit.set()
    assert await reaping == ["1000"]
    await closing


@pytest.mark.asyncio
async def test_close_tolerates_a_process_that_exits_before_terminate(tmp_path: Path, clock: Clock):
    """ProcessLookupError during a raced terminate means the child is already gone, not shutdown failure."""
    from sag_api.fnos.supervisor import WorkerSupervisor

    supervisor = WorkerSupervisor(
        tmp_path / "data",
        tmp_path / "tmp",
        process_factory=SingleProcessFactory(VanishingProcess()),
        clock=clock.now,
    )
    await supervisor.acquire(GatewayIdentity(1000, "Alice", False))

    await supervisor.close()


@pytest.mark.asyncio
async def test_close_tolerates_a_process_that_exits_during_graceful_wait(tmp_path: Path, clock: Clock):
    """A child disappearing after TERM must not make the graceful shutdown path fail."""
    from sag_api.fnos.supervisor import WorkerSupervisor

    process = VanishingWaitProcess()
    supervisor = WorkerSupervisor(
        tmp_path / "data",
        tmp_path / "tmp",
        process_factory=SingleProcessFactory(process),
        clock=clock.now,
    )
    await supervisor.acquire(GatewayIdentity(1000, "Alice", False))

    await supervisor.close()

    assert process.signals == ["TERM"]


@pytest.mark.asyncio
async def test_idle_worker_stops_with_term_before_kill(supervisor, clock):
    """A successful graceful stop must not escalate to KILL."""
    lease = await supervisor.acquire(GatewayIdentity(1000, "Alice", False))
    process = lease.handle.process
    await lease.release()
    clock.advance(901)

    assert await supervisor.reap_idle(clock.now()) == ["1000"]
    assert process.signals == ["TERM"]


@pytest.mark.asyncio
async def test_idle_worker_is_killed_after_grace_period(tmp_path: Path, clock: Clock):
    """A process that ignores TERM must be force-stopped after the configured grace period."""
    from sag_api.fnos.supervisor import WorkerSupervisor

    factory = FakeFactory(exit_after_term=False)
    supervisor = WorkerSupervisor(
        tmp_path / "data",
        tmp_path / "tmp",
        process_factory=factory,
        clock=clock.now,
    )
    lease = await supervisor.acquire(GatewayIdentity(1000, "Alice", False))
    process = lease.handle.process
    await lease.release()
    clock.advance(901)

    assert await supervisor.reap_idle(clock.now()) == ["1000"]
    assert process.signals == ["TERM", "KILL"]


@pytest.mark.asyncio
async def test_close_stops_every_known_worker(supervisor):
    """Leaving registry handles alive on shutdown would orphan child workers."""
    first = await supervisor.acquire(GatewayIdentity(1000, "Alice", False))
    second = await supervisor.acquire(GatewayIdentity(1001, "Bob", False))
    first_process = first.handle.process
    second_process = second.handle.process

    await supervisor.close()

    assert first_process.signals == ["TERM"]
    assert second_process.signals == ["TERM"]


@pytest.mark.asyncio
async def test_worker_status_requires_fnos_signature_and_counts_active_jobs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Returning all jobs, or accepting other auth modes, would make reaping unsafe."""
    import httpx

    from sag_api.fnos.identity import InternalIdentitySigner

    secret_path = tmp_path / "identity.key"
    secret_path.write_text("c" * 64, encoding="ascii")
    secret_path.chmod(0o600)
    signer = InternalIdentitySigner.from_file(secret_path)
    monkeypatch.setitem(settings.__dict__, "auth_mode", "fnos")
    monkeypatch.setitem(settings.__dict__, "fnos_uid", 1000)
    monkeypatch.setitem(settings.__dict__, "fnos_internal_secret_file", str(secret_path))

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(User.__table__.create)
        await connection.run_sync(Job.__table__.create)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session:
        session.add_all(
            [
                Job(type=JobType.PROCESS_DOCUMENT, status=JobStatus.QUEUED),
                Job(type=JobType.SYNC_SOURCE, status=JobStatus.RUNNING),
                Job(type=JobType.INDEX_UNIVERSE, status=JobStatus.SUCCEEDED),
            ]
        )
        await session.commit()

    async def session_override():
        async with sessions() as session:
            yield session

    app = FastAPI()
    from sag_api.api.v1.fnos_internal import router

    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[get_session] = session_override

    @app.exception_handler(ApiError)
    async def api_error(_request: Request, error: ApiError):
        return JSONResponse(status_code=error.status_code, content={"code": error.code})

    job_queries: list[str] = []

    def record_job_queries(_connection, _cursor, statement, _parameters, _context, _executemany):
        if "FROM jobs" in statement:
            job_queries.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", record_job_queries)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        missing = await client.get("/api/v1/fnos-internal/worker-status")
        signed = await client.get(
            "/api/v1/fnos-internal/worker-status",
            headers=signer.sign(
                GatewayIdentity(1000, "Alice", False), "status-1", int(time.time())
            ),
        )
        monkeypatch.setitem(settings.__dict__, "auth_mode", "legacy")
        disabled = await client.get("/api/v1/fnos-internal/worker-status")

    assert missing.status_code == 401
    assert signed.json() == {"queued": 1, "running": 1, "active": 2}
    assert disabled.status_code == 404
    assert len(job_queries) == 1
    event.remove(engine.sync_engine, "before_cursor_execute", record_job_queries)
    await engine.dispose()
