"""Lifecycle management for isolated fnOS user workers."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

import httpx

from sag_api.fnos.identity import GatewayIdentity, InternalIdentitySigner
from sag_api.fnos.workspace import WorkspacePaths, tenant_key

MAX_WORKERS = 4
IDLE_TIMEOUT_SECONDS = 15 * 60
GRACEFUL_STOP_SECONDS = 15
READY_TIMEOUT_SECONDS = 60
log = logging.getLogger(__name__)


class WorkerCapacityError(Exception):
    """All available fnOS worker slots are occupied."""

    status_code = 503
    retry_after = 5


class WorkerStartError(RuntimeError):
    """A per-user worker could not become ready; callers may retry safely."""

    def __init__(self, phase: str, cause: BaseException) -> None:
        super().__init__(f"fnOS worker startup failed during {phase}")
        self.phase = phase
        self.cause = cause


@dataclass
class WorkerHandle:
    uid: int
    key: str
    process: Any
    socket_file: Path
    client: Any
    identity: GatewayIdentity
    last_activity: float
    in_flight: int = 0
    streams: int = 0


@dataclass
class _StartingWorker:
    identity: GatewayIdentity
    paths: WorkspacePaths
    handle: WorkerHandle | None = None
    done: asyncio.Event | None = None

    def __post_init__(self) -> None:
        self.done = asyncio.Event()


class WorkerProcessFactory(Protocol):
    async def spawn(self, identity: GatewayIdentity, paths: WorkspacePaths) -> tuple[Any, Any]: ...


class SubprocessWorkerFactory:
    """Starts the real UDS worker with a client bound to its private socket."""

    async def spawn(self, identity: GatewayIdentity, paths: WorkspacePaths) -> tuple[Any, httpx.AsyncClient]:
        worker_env = os.environ.copy()
        worker_env["SAG_FNOS_DATA_ROOT"] = str(paths.data_root)
        worker_env["SAG_FNOS_TEMP_ROOT"] = str(paths.temp_root)
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "sag_api.fnos.worker",
            "--uid",
            str(identity.uid),
            "--username",
            identity.username,
            "--socket",
            str(paths.socket_file),
            env=worker_env,
            start_new_session=True,
        )
        client = httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(uds=str(paths.socket_file)),
            base_url="http://worker",
            # Cold-worker provisioning (first-time LanceDB dir + tokenizer + engine
            # schema) can take ~10s. Default httpx read timeout is 5s and produces
            # a 500 ReadTimeout at the gateway. Give write-path requests headroom
            # while keeping connect/pool checks tight.
            timeout=httpx.Timeout(connect=5.0, read=60.0, write=30.0, pool=5.0),
        )
        return process, client


class WorkerLease:
    """One in-flight request reservation for a worker."""

    def __init__(self, supervisor: WorkerSupervisor, handle: WorkerHandle) -> None:
        self._supervisor = supervisor
        self.handle = handle
        self._released = False

    @property
    def streams(self) -> int:
        return self.handle.streams

    @streams.setter
    def streams(self, value: int) -> None:
        if value < 0:
            raise ValueError("worker stream count cannot be negative")
        self.handle.streams = value

    async def __aenter__(self) -> WorkerLease:
        return self

    async def __aexit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
        await self.release()

    async def release(self) -> None:
        if self._released:
            return
        self._released = True
        await self._supervisor._release(self.handle)


class WorkerSupervisor:
    """Creates at most four tenant-scoped workers and safely reaps idle ones."""

    def __init__(
        self,
        data_root: Path,
        temp_root: Path,
        *,
        process_factory: WorkerProcessFactory | None = None,
        identity_signer: InternalIdentitySigner | None = None,
        clock: Callable[[], float] = time.monotonic,
        max_workers: int = MAX_WORKERS,
        idle_timeout: float = IDLE_TIMEOUT_SECONDS,
    ) -> None:
        self.data_root = Path(data_root)
        self.temp_root = Path(temp_root)
        self.process_factory = process_factory or SubprocessWorkerFactory()
        self._clock = clock
        self._max_workers = max_workers
        self._idle_timeout = idle_timeout
        self._identity_signer = identity_signer or self._configured_identity_signer()
        self._registry_lock = asyncio.Lock()
        self._workers: dict[str, WorkerHandle] = {}
        self._starting: dict[str, _StartingWorker] = {}
        self._stopping: dict[str, WorkerHandle] = {}
        self._teardown_tasks: dict[str, tuple[WorkerHandle, asyncio.Task[None]]] = {}
        self._start_locks: dict[str, asyncio.Lock] = {}
        self._closed = False

    @staticmethod
    def _configured_identity_signer() -> InternalIdentitySigner | None:
        from sag_api.core.config import settings

        if not settings.fnos_internal_secret_file:
            return None
        return InternalIdentitySigner.from_file(Path(settings.fnos_internal_secret_file))

    async def acquire(self, identity: GatewayIdentity) -> WorkerLease:
        """Return a lease, starting the identity's worker if necessary."""
        async with self._registry_lock:
            if self._closed:
                raise RuntimeError("worker supervisor is closed")
            key = tenant_key(identity.uid, identity.username)
            start_lock = self._start_locks.setdefault(key, asyncio.Lock())

        async with start_lock:
            async with self._registry_lock:
                existing = self._workers.get(key)
                if existing is not None:
                    existing.in_flight += 1
                    return WorkerLease(self, existing)
                if self._closed:
                    raise RuntimeError("worker supervisor is closed")
                if len(self._workers) + len(self._starting) + len(self._stopping) >= self._max_workers:
                    raise WorkerCapacityError()
                paths = WorkspacePaths.for_identity(self.data_root, self.temp_root, identity)
                starting = _StartingWorker(identity=identity, paths=paths)
                self._starting[key] = starting

            phase = "prepare"
            try:
                starting.paths.prepare()
                starting.paths.socket_file.unlink(missing_ok=True)
                phase = "spawn"
                process, client = await self.process_factory.spawn(identity, starting.paths)
                starting.handle = WorkerHandle(
                    uid=identity.uid,
                    key=key,
                    process=process,
                    socket_file=starting.paths.socket_file,
                    client=client,
                    identity=identity,
                    last_activity=self._clock(),
                    in_flight=1,
                )
                async with self._registry_lock:
                    if self._closed:
                        raise RuntimeError("worker supervisor is closed")
                phase = "ready"
                await self._wait_until_ready(process, client)
            except BaseException as error:
                if starting.handle is not None:
                    await self._stop_handle_once(starting.handle)
                else:
                    starting.paths.socket_file.unlink(missing_ok=True)
                async with self._registry_lock:
                    self._starting.pop(key, None)
                    self._discard_teardown_locked(starting.handle)
                    starting.done.set()
                if isinstance(error, asyncio.CancelledError):
                    raise
                log.warning(
                    "fnOS worker startup failed uid=%s key=%s phase=%s error=%s",
                    identity.uid,
                    key,
                    phase,
                    type(error).__name__,
                )
                raise WorkerStartError(phase, error) from error

            handle = starting.handle
            assert handle is not None
            async with self._registry_lock:
                self._starting.pop(key, None)
                if self._closed:
                    should_stop = True
                else:
                    self._workers[key] = handle
                    should_stop = False
                starting.done.set()
            if should_stop:
                await self._stop_handle_once(handle)
                async with self._registry_lock:
                    self._discard_teardown_locked(handle)
                raise RuntimeError("worker supervisor is closed")
            return WorkerLease(self, handle)

    async def _wait_until_ready(self, process: Any, client: Any) -> None:
        deadline = time.monotonic() + READY_TIMEOUT_SECONDS
        while True:
            if getattr(process, "returncode", None) is not None:
                raise RuntimeError("fnOS worker exited before becoming ready")
            try:
                response = await client.get("/api/v1/system/ready")
                response.raise_for_status()
                return
            except Exception:
                if time.monotonic() >= deadline:
                    raise TimeoutError("fnOS worker did not become ready within 60 seconds") from None
                await asyncio.sleep(0.1)

    async def _release(self, handle: WorkerHandle) -> None:
        async with self._registry_lock:
            if self._workers.get(handle.key) is not handle:
                return
            handle.in_flight = max(0, handle.in_flight - 1)
            handle.last_activity = self._clock()

    async def reap_idle(self, now: float) -> list[str]:
        """Stop workers that have been entirely idle for the configured timeout.

        Returns the tenant keys of the workers that were stopped.
        """
        async with self._registry_lock:
            candidates = [
                handle
                for handle in self._workers.values()
                if self._is_idle(handle, now)
            ]

        reaped: list[str] = []
        for handle in candidates:
            if await self._active_jobs(handle) != 0:
                continue
            async with self._registry_lock:
                start_lock = self._start_locks.setdefault(handle.key, asyncio.Lock())
            async with start_lock:
                async with self._registry_lock:
                    if self._workers.get(handle.key) is not handle or not self._is_idle(handle, now):
                        continue
                    self._workers.pop(handle.key)
                    self._stopping[handle.key] = handle
                try:
                    await self._stop_handle_once(handle)
                finally:
                    async with self._registry_lock:
                        if self._stopping.get(handle.key) is handle:
                            self._stopping.pop(handle.key)
                        self._discard_teardown_locked(handle)
                reaped.append(handle.key)
        return reaped

    def _is_idle(self, handle: WorkerHandle, now: float) -> bool:
        return (
            handle.in_flight == 0
            and handle.streams == 0
            and now - handle.last_activity >= self._idle_timeout
        )

    async def _active_jobs(self, handle: WorkerHandle) -> int:
        headers: dict[str, str] | None = None
        if self._identity_signer is not None:
            headers = self._identity_signer.sign(
                handle.identity,
                request_id=uuid4().hex,
                now=int(time.time()),
            )
        response = await handle.client.get("/api/v1/fnos-internal/worker-status", headers=headers)
        response.raise_for_status()
        status = response.json()
        return int(status["active"])

    async def close(self) -> None:
        """Stop every currently known child worker."""
        async with self._registry_lock:
            self._closed = True
            handles = list(self._workers.values())
            self._workers.clear()
            handles.extend(self._stopping.values())
            starting = list(self._starting.values())
        for handle in handles:
            await self._stop_handle_once(handle)
        for state in starting:
            if state.handle is not None:
                await self._stop_handle_once(state.handle)
        await asyncio.gather(*(state.done.wait() for state in starting))

    def _discard_teardown_locked(self, handle: WorkerHandle | None) -> None:
        if handle is None:
            return
        current = self._teardown_tasks.get(handle.key)
        if current is not None and current[0] is handle and current[1].done():
            self._teardown_tasks.pop(handle.key)

    async def _stop_handle_once(self, handle: WorkerHandle) -> None:
        async with self._registry_lock:
            current = self._teardown_tasks.get(handle.key)
            if current is not None and current[0] is handle:
                task = current[1]
            else:
                task = asyncio.create_task(self._stop_handle(handle))
                self._teardown_tasks[handle.key] = (handle, task)
        await asyncio.shield(task)

    async def _stop_handle(self, handle: WorkerHandle) -> None:
        await self._close_client(handle.client)
        await self._stop_process(handle.process)
        handle.socket_file.unlink(missing_ok=True)

    @staticmethod
    async def _close_client(client: Any | None) -> None:
        if client is not None:
            await client.aclose()

    @staticmethod
    async def _stop_process(process: Any | None) -> None:
        if process is None or getattr(process, "returncode", None) is not None:
            return
        try:
            process.terminate()
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(process.wait(), timeout=GRACEFUL_STOP_SECONDS)
        except ProcessLookupError:
            return
        except TimeoutError:
            try:
                process.kill()
            except ProcessLookupError:
                return
            try:
                await process.wait()
            except ProcessLookupError:
                return
