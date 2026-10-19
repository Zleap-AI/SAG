"""Generate authentic two-tenant fnOS stores using zleap-sag 0.13.0.

Run this file with the old API environment and its matching source on PYTHONPATH.
Only the external embedding and LLM response boundaries are deterministic; the
parser, validation, pipeline, SQLite writers and LanceDB writers remain real.
"""

import argparse
import asyncio
import hashlib
import importlib.metadata
import json
import sqlite3
import tarfile
from pathlib import Path

from openpyxl import Workbook
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from zleap.sag import DataEngine, EngineConfig
from zleap.sag.config import EmbeddingConfig, LLMConfig
from zleap.sag.core.ai.embedding import EmbeddingClient
from zleap.sag.db import DataSource
from zleap.sag.modules.extract.processor import EventProcessor

from sag_api.core.config import Settings
from sag_api.db.base import Base
from sag_api.db.models import Agent, Document, Job, Message, Setting, Source, Thread, User
from sag_api.enums import DocumentStatus, JobStatus, JobType, MessageRole
from sag_api.fnos.knowledge_upgrade import reset_legacy_knowledge
from sag_api.parsing import prepare_document
from sag_api.sag.dto import ProcessCheckpoint
from sag_api.sag.incremental_processor import IncrementalDocumentProcessor

BASELINE = "99510145db9b6da0f1a533c89c7c3e17560d8f8b"
PREFIX = "/fixture-native-013"
TENANTS = ("1000", "1001-bob-81b637d8")


async def fixed_embedding(self, text):
    return [1.0] + [0.0] * 15


async def fixed_batch_embedding(self, texts):
    return [await fixed_embedding(self, text) for text in texts]


async def fixed_response(self, messages, schema, **kwargs):
    # Still passes the real dynamic entity and rich-reference validators.
    entity_type = self.entity_types[0].type
    return {
        "type": "response",
        "data": {
            "items": [
                {
                    "reason": "The record states retained private knowledge.",
                    "title": "Retained knowledge",
                    "summary": "Private knowledge survives the SDK upgrade.",
                    "content": "Private knowledge survives the SDK upgrade.",
                    "references": [1],
                    "entities": [{"type": entity_type, "name": "Migration", "description": "Retained knowledge"}],
                }
            ]
        },
    }


def install_model_boundaries(monkeypatch=None):
    assign = monkeypatch.setattr if monkeypatch else setattr
    assign(EmbeddingClient, "generate", fixed_embedding)
    assign(EmbeddingClient, "batch_generate", fixed_batch_embedding)
    assign(EventProcessor, "_chat_with_schema_once", fixed_response)


def config(workspace):
    return EngineConfig(
        storage_mode="normal",
        data_dir=str(workspace / "engine-v0.13-clean"),
        language="en",
        llm=LLMConfig(api_key="fixture-only", model="openai/fixture"),
        embedding=EmbeddingConfig(model="fixture", api_key="fixture-only", schema_dimensions=16),
    )


async def never_pause():
    return False


async def build(root):
    assert importlib.metadata.version("zleap-sag") == "0.13.0"
    install_model_boundaries()
    states = {}
    for tenant in TENANTS:
        workspace = root / "users" / tenant
        uploads = workspace / "uploads" / f"source-{tenant}"
        uploads.mkdir(parents=True)
        (workspace / "engine").mkdir()
        (workspace / "engine" / "legacy-retained.txt").write_text("Retained pre-0.13 recovery directory\n")
        book = uploads / "book.md"
        book.write_text(f"# Tenant {tenant}\n\n" + f"Private knowledge of {tenant} survives upgrade. " * 12)
        paused = uploads / "paused.md"
        paused.write_text(f"# Paused {tenant}\n\n" + "Resume the original indexed chunks without another ingest. " * 12)
        spreadsheet = uploads / "records.xlsx"
        workbook = Workbook()
        workbook.active.append(["Record", "Owner", "Amount"])
        workbook.active.append(["R001", tenant, 100.25])
        workbook.active.append(["R002", tenant, 200.5])
        workbook.save(spreadsheet)
        canonical = await prepare_document(str(spreadsheet), Settings(_env_file=None, document_parser="markitdown"))
        checkpoints = {}
        outcomes = {}
        async with DataEngine(config(workspace), data_source_id=f"config-{tenant}") as engine:
            async with engine.resources.relational.session_factory()() as session:
                session.add(DataSource(id=f"config-{tenant}", name=f"Knowledge {tenant}"))
                await session.commit()
            processor = IncrementalDocumentProcessor(engine, f"config-{tenant}", max_concurrency=1)
            for name, path, original in (("book", book, None), ("sheet", canonical.path, spreadsheet)):

                async def save(checkpoint, name=name, target=checkpoints):
                    target[name] = checkpoint.model_dump(mode="json")

                outcomes[name] = await processor.process(
                    path,
                    checkpoint=ProcessCheckpoint(),
                    on_checkpoint=save,
                    should_pause=never_pause,
                    original_path=original,
                )
                assert outcomes[name].chunk_count > 0 and outcomes[name].event_count > 0

            # Persist the actual app ingest boundary: source/chunk versions and
            # IDs all come from SDK 0.13 rather than being manufactured as SQL.
            async def save_paused(checkpoint, target=checkpoints):
                target["paused"] = checkpoint.model_dump(mode="json")

            await processor._ingest(ProcessCheckpoint(), paused, save_paused)

        meta = workspace / "meta" / "sag.db"
        meta.parent.mkdir()
        database = create_async_engine(f"sqlite+aiosqlite:///{meta}")
        async with database.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(database, expire_on_commit=False)
        async with sessions() as session:
            await reset_legacy_knowledge(session, workspace / "engine")
            source = Source(id=f"source-{tenant}", name=f"Knowledge {tenant}", sag_source_config_id=f"config-{tenant}")
            user = User(id=f"user-{tenant}", email=f"{tenant}@fixture.test", password_hash="fixture-only")
            agent = Agent(id=f"agent-{tenant}", name="Retained agent", persona={"greeting": "hello"})
            session.add_all([source, user, agent])
            await session.flush()
            for name, path in (("book", book), ("sheet", spreadsheet), ("paused", paused)):
                checkpoint = checkpoints[name]
                session.add(
                    Document(
                        id=f"{name}-{tenant}",
                        source_id=source.id,
                        filename=path.name,
                        storage_path=str(path).replace(str(root), PREFIX),
                        status=DocumentStatus.PAUSED if name == "paused" else DocumentStatus.READY,
                        sag_source_id=checkpoint["source_id"],
                        chunk_count=len(checkpoint["chunk_ids"]),
                        event_count=checkpoint["event_count"],
                    )
                )
            thread = Thread(id=f"thread-{tenant}", agent_id=agent.id, title="Retained conversation")
            session.add(thread)
            await session.flush()
            session.add_all(
                [
                    Job(
                        id=f"job-{tenant}",
                        source_id=source.id,
                        document_id=f"paused-{tenant}",
                        type=JobType.PROCESS_DOCUMENT,
                        status=JobStatus.PAUSED,
                        payload={"process_checkpoint": checkpoints["paused"]},
                    ),
                    Message(
                        id=f"message-{tenant}",
                        thread_id=thread.id,
                        role=MessageRole.ASSISTANT,
                        content="Retained answer [1]",
                        citations=[
                            {
                                "n": 1,
                                "chunk_id": checkpoints["book"]["chunk_ids"][0],
                                "source_id": source.id,
                            }
                        ],
                    ),
                    Setting(
                        scope="global",
                        key="model_config",
                        value={
                            "llm_model": "fixture-model",
                            "embedding_model": "fixture",
                            "embedding_schema_dimensions": 16,
                            "llm_api_key": f"fixture-only-{tenant}",
                            "search_strategy": "vector",
                        },
                    ),
                ]
            )
            await session.commit()
        await database.dispose()
        for sqlite_file in workspace.rglob("*.db"):
            with sqlite3.connect(sqlite_file) as connection:
                connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        states[tenant] = checkpoints
    return states


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    states = asyncio.run(build(args.root))
    files = {
        str(p.relative_to(args.root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(args.root.rglob("*"))
        if p.is_file()
    }
    args.output.mkdir(parents=True, exist_ok=True)
    archive_path = args.output / "native-013.tar.gz"
    with tarfile.open(archive_path, "w:gz") as archive:
        archive.add(args.root / "users", arcname="users")
    (args.output / "provenance.json").write_text(
        json.dumps(
            {
                "baseline": BASELINE,
                "engine_version": "0.13.0",
                "path_prefix": PREFIX,
                "tenants": TENANTS,
                "checkpoints": states,
                "files": files,
                "dependency_versions": {
                    name: importlib.metadata.version(name) for name in ("zleap-sag", "lancedb", "pyarrow", "sqlalchemy")
                },
                "archive_sha256": hashlib.sha256(archive_path.read_bytes()).hexdigest(),
                "generator_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                "model_boundary": (
                    "Deterministic embedding and LLM response only; real old validators, parser and storage writers"
                ),
            },
            indent=2,
        )
        + "\n"
    )


if __name__ == "__main__":
    main()
