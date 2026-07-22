"""会话列表分页：最近优先、页间无重复、参数受约束。"""

import httpx
import pytest


async def _register(client: httpx.AsyncClient) -> dict[str, str]:
    response = await client.post(
        "/api/v1/auth/register",
        json={"email": "thread-pagination@t.com", "password": "password123"},
    )
    assert response.status_code == 201, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


@pytest.mark.asyncio
async def test_archived_threads_support_stable_offset_pagination():
    from sag_api.main import app

    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            headers = await _register(client)
            agent = (await client.get("/api/v1/agents/default", headers=headers)).json()

            for index in range(7):
                created = await client.post(
                    f"/api/v1/agents/{agent['id']}/threads",
                    headers=headers,
                    json={"title": f"分页会话 {index}"},
                )
                assert created.status_code == 201, created.text
                archived = await client.patch(
                    f"/api/v1/agents/{agent['id']}/threads/{created.json()['id']}",
                    headers=headers,
                    json={"archived": True},
                )
                assert archived.status_code == 200, archived.text

            base_url = f"/api/v1/agents/{agent['id']}/threads?archived=true"
            default_rows = (await client.get(base_url, headers=headers)).json()
            all_rows = (await client.get(f"{base_url}&limit=100", headers=headers)).json()
            first = (await client.get(f"{base_url}&limit=3", headers=headers)).json()
            second = (await client.get(f"{base_url}&limit=3&offset=3", headers=headers)).json()

            assert len(all_rows) >= 7
            assert default_rows == all_rows[:6]
            assert first == all_rows[:3]
            assert second == all_rows[3:6]
            assert {row["id"] for row in first}.isdisjoint(row["id"] for row in second)
            assert [(row["updated_at"], row["id"]) for row in all_rows] == sorted(
                ((row["updated_at"], row["id"]) for row in all_rows),
                reverse=True,
            )

            invalid = await client.get(f"{base_url}&limit=0", headers=headers)
            assert invalid.status_code == 422
