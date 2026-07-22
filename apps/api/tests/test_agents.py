"""Agent HTTP e2e（离线）：CRUD、信源绑定、会话、ask 守卫、绑定→信源解析。"""

import httpx
import pytest


@pytest.mark.asyncio
async def test_agents_flow_offline():
    from sag_api.core.db import SessionLocal
    from sag_api.db.models import Agent
    from sag_api.main import app
    from sag_api.services.agent_domain import resolve_sources

    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            tok = (
                await c.post(
                    "/api/v1/auth/register", json={"email": "agent@x.com", "password": "password123"}
                )
            ).json()["access_token"]
            H = {"Authorization": f"Bearer {tok}"}

            src = (await c.post("/api/v1/sources", headers=H, json={"name": "手册"})).json()
            scoped_src = (
                await c.post("/api/v1/sources", headers=H, json={"name": "临时范围"})
            ).json()

            # 建 agent
            r = await c.post(
                "/api/v1/agents",
                headers=H,
                json={"name": "阿默", "avatar": "阿", "persona": {"system_prompt": "你是阿默。"}},
            )
            assert r.status_code == 201
            agent = r.json()
            assert agent["name"] == "阿默"
            aid = agent["id"]

            # 绑定信源
            b = await c.post(
                f"/api/v1/agents/{aid}/bindings",
                headers=H,
                json={"target_type": "source", "target_id": src["id"]},
            )
            assert b.status_code == 201
            assert len((await c.get(f"/api/v1/agents/{aid}/bindings", headers=H)).json()) == 1
            # 重复绑定 → 409
            assert (
                await c.post(
                    f"/api/v1/agents/{aid}/bindings",
                    headers=H,
                    json={"target_type": "source", "target_id": src["id"]},
                )
            ).status_code == 409

            # 绑定 → 信源解析
            async with SessionLocal() as s:
                agent_obj = await s.get(Agent, aid)
                resolved = await resolve_sources(s, agent_obj)
                explicitly_scoped = await resolve_sources(s, agent_obj, [scoped_src["id"]])
            assert [x.id for x in resolved] == [src["id"]]
            assert [x.id for x in explicitly_scoped] == [scoped_src["id"]]

            # 启动 run 前的配置错误使用标准 HTTP 错误，不伪装成 SSE run。
            th = (await c.post(f"/api/v1/agents/{aid}/threads", headers=H, json={})).json()
            ask = await c.post(
                f"/api/v1/agents/{aid}/threads/{th['id']}/ask", headers=H, json={"query": "你好"}
            )
            assert ask.status_code == 400
            assert ask.json()["error"]["code"] == "configuration_error"

            # 列表 + 删除（共享测试库 → 用存在性断言而非精确计数）
            assert any(a["id"] == aid for a in (await c.get("/api/v1/agents", headers=H)).json())
            assert (await c.delete(f"/api/v1/agents/{aid}", headers=H)).status_code == 200
            assert not any(a["id"] == aid for a in (await c.get("/api/v1/agents", headers=H)).json())
