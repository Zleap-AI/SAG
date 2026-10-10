"""Subprocess driver: real queue, public SDK, and isolated persistent stores."""

import asyncio
import json
import sys
from pathlib import Path
from uuid import uuid4

from zleap.sag import DataEngine
from zleap.sag.modules.extract.saver import EventSaver
from zleap.sag.operations import OperationContext

from sag_api.core.config import settings
from sag_api.core.db import SessionLocal, engine, init_db
from sag_api.db.models import Document, Job, Source
from sag_api.enums import DocumentStatus, JobStatus, JobType
from sag_api.jobs import tasks
from sag_api.jobs.inproc import InProcessAsyncQueue
from sag_api.sag import EngineManager
from sag_api.sag import engine_manager as manager_module
from sag_api.services import universe_service
from sag_api.services.document_service import read_document_lines, resume_document


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


async def main():
    root, mode, kill_at = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
    root.mkdir(parents=True, exist_ok=True)
    phase = mode

    def trace(method):
        with (root / "sdk-calls.jsonl").open("a") as output:
            output.write(json.dumps({"phase": phase, "method": method}) + "\n")

    for method in ("get_operation_status", "process_source", "delete_source", "delete_data_source"):
        original = getattr(DataEngine, method)

        async def called(self, *args, _method=method, _original=original, **kwargs):
            trace(_method)
            return await _original(self, *args, **kwargs)

        setattr(DataEngine, method, called)

    original_commit = EventSaver.commit

    async def commit(self, *args, **kwargs):
        trace("commit")
        return await original_commit(self, *args, **kwargs)

    EventSaver.commit = commit

    async def observer(event):
        if mode == "create" and kill_at == "extract_prepared" and event.kind == "checkpoint":
            if event.checkpoint == "extract_prepared":
                write(root / "prepared.json", {"checkpoint": event.checkpoint})
                await asyncio.Event().wait()

    def actual_engine(*args, **kwargs):
        return DataEngine(*args, observer=observer, **kwargs)

    manager_module.DataEngine = actual_engine
    # Public SDK lease configuration only. Business leases retain the real
    # 60-second default; no clock/table/checkpoint is changed after SIGKILL.
    tasks.OperationContext = lambda **kwargs: OperationContext(**kwargs, lease_seconds=30)

    async def no_projection(*args, **kwargs):
        return []

    universe_service.schedule_universe_refresh = no_projection
    await init_db()
    manager = EngineManager(settings)
    queue = InProcessAsyncQueue(SessionLocal, manager, concurrency=2)
    try:
        await queue.start()
        if mode == "create":
            write(root / "settings.json", settings.model_dump(mode="json"))
            async with SessionLocal() as session:
                source = Source(name="SIGKILL acceptance", sag_source_config_id=uuid4().hex)
                session.add(source)
                await session.flush()
                documents, jobs = [], []
                for name in ("a", "b"):
                    path = root / f"{name}.md"
                    text = "\n\n".join(
                        f"# Milestone {i}\nAcme Corporation completed milestone {i}. "
                        + f"Milestone {i} has an independently verified operational result. " * 20
                        for i in range(6)
                    ) if name == "a" else "# Peer\nAcme Corporation completed the peer rollout."
                    path.write_text(text, encoding="utf-8")
                    document = Document(
                        source_id=source.id, filename=path.name, storage_path=str(path),
                        status=DocumentStatus.PENDING if name == "a" else DocumentStatus.PAUSED,
                    )
                    session.add(document)
                    await session.flush()
                    job = Job(
                        source_id=source.id, document_id=document.id, type=JobType.PROCESS_DOCUMENT,
                        status=JobStatus.QUEUED if name == "a" else JobStatus.PAUSED,
                        payload={"control_revision": 1},
                    )
                    session.add(job)
                    await session.flush()
                    documents.append(document.id)
                    jobs.append(job.id)
                await session.commit()
                await manager.provision(source.sag_source_config_id, source)
                write(root / "identities.json", {
                    "source": source.id, "range": source.sag_source_config_id,
                    "documents": documents, "jobs": jobs,
                })
            await queue.enqueue(jobs[0])
            await asyncio.Event().wait()

        identities = json.loads((root / "identities.json").read_text())
        async with SessionLocal() as session:
            documents = [await session.get(Document, identity) for identity in identities["documents"]]
            jobs = [await session.get(Job, identity) for identity in identities["jobs"]]
            write(root / "startup.json", {
                "documents": [{"status": document.status.value, "published": document.sag_source_id}
                              for document in documents],
                "jobs": [{"status": job.status.value, "payload": job.payload} for job in jobs],
            })
        while not (root / "command.json").exists():
            await asyncio.sleep(0.05)
        for index in json.loads((root / "command.json").read_text())["resume"]:
            phase = f"resume-{index}"
            async with SessionLocal() as session:
                resumed = await resume_document(
                    session, await session.get(Source, identities["source"]), identities["documents"][index],
                    job_queue=queue, engine_manager=manager,
                )
                assert resumed.id == identities["jobs"][index]
            async with asyncio.timeout(150):
                while True:
                    async with SessionLocal() as session:
                        job = await session.get(Job, identities["jobs"][index])
                        if job.status == JobStatus.SUCCEEDED:
                            document = await session.get(Document, identities["documents"][index])
                            source = await session.get(Source, identities["source"])
                            body = await read_document_lines(document, source, manager)
                            write(root / f"completed-{index}.json", {
                                "job": job.id, "payload": job.payload, "input": document.sag_source_id,
                                "operation": document.published_operation_id, "body": body,
                                "chunks": document.chunk_count, "events": document.event_count,
                            })
                            break
                        assert job.status not in {JobStatus.FAILED, JobStatus.PAUSED}, job.error
                    await asyncio.sleep(0.1)
        write(root / "finished.json", {"ok": True})
    finally:
        await queue.stop()
        await manager.aclose_all()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
