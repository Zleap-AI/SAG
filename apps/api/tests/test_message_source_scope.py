"""Explicit chat scope survives history playback independently of retrieval/citations."""

import json

import httpx
import pytest
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import create_async_engine

from sag_agent import ModelChunk


class DirectLLM:
    configured = True

    async def stream_turn(self, request, cancellation):
        cancellation.raise_if_cancelled()
        yield ModelChunk(text_delta="Reply", finish_reason="stop")


@pytest.mark.asyncio
async def test_selected_scope_is_saved_and_survives_source_rename_and_deletion():
    from sag_api.core.db import SessionLocal
    from sag_api.db.models import Message, Source
    from sag_api.enums import MessageRole
    from sag_api.main import app

    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        app.state.llm = DirectLLM()
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            registered = await client.post(
                "/api/v1/auth/register", json={"email": "scope@t.com", "password": "password123"},
            )
            assert registered.status_code == 201, registered.text
            headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}
            agent = (await client.get("/api/v1/agents/default", headers=headers)).json()
            thread = (await client.post(
                f"/api/v1/agents/{agent['id']}/threads", headers=headers, json={},
            )).json()
            base = f"/api/v1/agents/{agent['id']}/threads/{thread['id']}"

            # No provisioning or model transport is required for source selection metadata.
            async with SessionLocal() as session:
                first = Source(name="Original name", sag_source_config_id="scope-test-first")
                second = Source(name="Second source", sag_source_config_id="scope-test-second")
                session.add_all([first, second])
                await session.commit()
                first_id, second_id = first.id, second.id

            expected = [{"id": second_id, "name": "Second source"}, {"id": first_id, "name": "Original name"}]
            scoped = await client.post(
                f"{base}/ask", headers=headers,
                json={"query": "Scoped question", "source_ids": [second_id, first_id, second_id]},
            )
            assert scoped.status_code == 200, scoped.text
            started = next(
                json.loads(line.removeprefix("data: "))["payload"]
                for line in scoped.text.splitlines()
                if line.startswith("data:") and json.loads(line.removeprefix("data: "))["type"] == "run.started"
            )
            assert started["source_scope"] == expected

            default = await client.post(f"{base}/ask", headers=headers, json={"query": "Default question"})
            assert default.status_code == 200, default.text
            # Default resolution may see sources, but explicit selection remains empty.
            default_started = next(
                json.loads(line.removeprefix("data: "))["payload"]
                for line in default.text.splitlines()
                if line.startswith("data:") and json.loads(line.removeprefix("data: "))["type"] == "run.started"
            )
            assert default_started["sources"]
            assert default_started["source_scope"] == []

            async with SessionLocal() as session:
                first = await session.get(Source, first_id)
                first.name = "Renamed"
                await session.delete(await session.get(Source, second_id))
                session.add(Message(thread_id=thread["id"], role=MessageRole.USER, content="Legacy question"))
                await session.commit()

            history = await client.get(f"{base}/messages", headers=headers)
            assert history.status_code == 200, history.text
            messages = {item["content"]: item for item in history.json()["items"] if item["role"] == "user"}
            assert messages["Scoped question"]["source_scope"] == expected
            assert messages["Default question"]["source_scope"] == []
            assert messages["Legacy question"]["source_scope"] is None
            assert (await client.get(f"{base}/messages")).status_code == 401

            # Retrying deleted IDs retains explicit selection instead of widening to defaults.
            retry = await client.post(
                f"{base}/ask", headers=headers, json={"query": "Retry deleted source", "source_ids": [second_id]},
            )
            assert retry.status_code == 200, retry.text
            retried = (await client.get(f"{base}/messages", headers=headers)).json()["items"]
            retry_user = next(item for item in retried if item["content"] == "Retry deleted source")
            assert retry_user["source_scope"] == [{"id": second_id, "name": second_id}]


@pytest.mark.asyncio
async def test_source_scope_column_upgrade_is_idempotent_and_preserves_legacy_rows(tmp_path, monkeypatch):
    from sag_api.core import db

    isolated = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/legacy.db")
    monkeypatch.setattr(db, "engine", isolated)
    try:
        async with isolated.begin() as connection:
            await connection.execute(text("CREATE TABLE messages (id TEXT PRIMARY KEY, content TEXT)"))
            await connection.execute(text("INSERT INTO messages (id, content) VALUES ('legacy', 'Question')"))
        await db._ensure_columns()
        await db._ensure_columns()
        async with isolated.connect() as connection:
            columns = await connection.run_sync(lambda sync: inspect(sync).get_columns("messages"))
            assert [column["name"] for column in columns].count("source_scope_json") == 1
            legacy = (await connection.execute(text("SELECT content, source_scope_json FROM messages"))).one()
            assert legacy == ("Question", None)
    finally:
        await isolated.dispose()
