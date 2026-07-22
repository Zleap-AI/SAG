"""消息历史的有界 keyset 分页与 Agent 上下文装载上限。"""

from datetime import UTC, datetime

import httpx
import pytest

from sag_api.core.config import settings
from sag_api.core.db import SessionLocal
from sag_api.db.models import Message
from sag_api.enums import MessageRole
from sag_api.services.agent_domain import _history


def test_message_history_declares_keyset_index():
    index = next(
        item for item in Message.__table__.indexes if item.name == "ix_messages_thread_created_id"
    )
    assert tuple(column.name for column in index.columns) == ("thread_id", "created_at", "id")


async def _register(client: httpx.AsyncClient) -> dict[str, str]:
    response = await client.post(
        "/api/v1/auth/register",
        json={"email": "message-pagination@t.com", "password": "password123"},
    )
    assert response.status_code == 201, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


@pytest.mark.asyncio
async def test_messages_use_signed_bounded_keyset_pages(monkeypatch):
    from sag_api.main import app

    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            headers = await _register(client)
            agent = (await client.get("/api/v1/agents/default", headers=headers)).json()
            thread = (
                await client.post(
                    f"/api/v1/agents/{agent['id']}/threads",
                    headers=headers,
                    json={"title": "消息分页"},
                )
            ).json()
            other_thread = (
                await client.post(
                    f"/api/v1/agents/{agent['id']}/threads",
                    headers=headers,
                    json={"title": "其他会话"},
                )
            ).json()

            # 所有消息共用时间戳，强制分页依赖 id 作稳定的第二排序键。
            created_at = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
            async with SessionLocal() as session:
                session.add_all(
                    [
                        Message(
                            id=f"{10_000 + index:032x}",
                            thread_id=thread["id"],
                            role=(
                                MessageRole.USER if index % 2 == 0 else MessageRole.ASSISTANT
                            ),
                            content=f"message-{index:03d}",
                            citations=[],
                            attachments=[],
                            steps=[],
                            created_at=created_at,
                        )
                        for index in range(105)
                    ]
                )
                await session.commit()

            base = f"/api/v1/agents/{agent['id']}/threads/{thread['id']}/messages"
            first_response = await client.get(base, headers=headers)
            assert first_response.status_code == 200, first_response.text
            first = first_response.json()
            assert set(first) == {"items", "next_cursor", "has_more"}
            assert [item["content"] for item in first["items"]] == [
                f"message-{index:03d}" for index in range(65, 105)
            ]
            assert first["has_more"] is True
            assert isinstance(first["next_cursor"], str)

            second = (
                await client.get(
                    base,
                    headers=headers,
                    params={"cursor": first["next_cursor"]},
                )
            ).json()
            third = (
                await client.get(
                    base,
                    headers=headers,
                    params={"cursor": second["next_cursor"]},
                )
            ).json()
            assert [item["content"] for item in second["items"]] == [
                f"message-{index:03d}" for index in range(25, 65)
            ]
            assert [item["content"] for item in third["items"]] == [
                f"message-{index:03d}" for index in range(25)
            ]
            assert second["has_more"] is True
            assert third["has_more"] is False
            assert third["next_cursor"] is None
            ids = [
                item["id"]
                for page in (third, second, first)
                for item in page["items"]
            ]
            assert len(ids) == len(set(ids)) == 105

            max_page = (await client.get(base, headers=headers, params={"limit": 100})).json()
            assert len(max_page["items"]) == 100
            assert max_page["has_more"] is True

            cursor = first["next_cursor"]
            tampered = f"{'A' if cursor[0] != 'A' else 'B'}{cursor[1:]}"
            invalid = await client.get(base, headers=headers, params={"cursor": tampered})
            assert invalid.status_code == 422
            assert invalid.json()["error"]["code"] == "invalid_cursor"

            wrong_scope = await client.get(
                f"/api/v1/agents/{agent['id']}/threads/{other_thread['id']}/messages",
                headers=headers,
                params={"cursor": cursor},
            )
            assert wrong_scope.status_code == 422
            assert wrong_scope.json()["error"]["code"] == "invalid_cursor"

            assert (await client.get(base, headers=headers, params={"limit": 0})).status_code == 422
            assert (await client.get(base, headers=headers, params={"limit": 101})).status_code == 422
            assert (
                await client.get(base, headers=headers, params={"cursor": "unsigned.invalid"})
            ).status_code == 422

            # 提示词历史是另一条有界查询：只装载最近 N 条，并保持正向顺序。
            monkeypatch.setattr(settings, "history_load_limit", 5)
            async with SessionLocal() as session:
                history = await _history(
                    session,
                    thread["id"],
                    exclude_id=f"{10_000 + 104:032x}",
                )
            assert [item["content"] for item in history] == [
                f"message-{index:03d}" for index in range(99, 104)
            ]
