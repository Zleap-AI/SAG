"""v0.3 客户端形态后端：默认 agent、近期动态、文档原文端点。全程离线。"""

import httpx
import pytest

from sag_agent import ModelChunk


class OfflineLLM:
    @property
    def configured(self):
        return True

    async def stream_turn(self, request, cancellation):
        yield ModelChunk(text_delta="ok", finish_reason="stop")

    async def complete(self, messages):
        return "摘要"


async def _register(c, email):
    r = await c.post("/api/v1/auth/register", json={"email": email, "password": "password123"})
    assert r.status_code == 201, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.mark.asyncio
async def test_default_agent_activity_and_document_file():
    from sqlalchemy import select

    from sag_api.core.db import SessionLocal
    from sag_api.db.models import Agent
    from sag_api.main import app
    from sag_api.services.agent_domain import resolve_sources

    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            A = await _register(c, "clientform@t.com")

            # 模拟旧版本完整默认值；正式端点应安全迁移到新身份。
            async with SessionLocal() as s:
                legacy = await s.scalar(select(Agent).where(Agent.is_default.is_(True)))
                legacy.name = "sag"
                legacy.avatar = "s"
                legacy.persona = {
                    "greeting": "我在。上传资料到知识库，或直接问我任何问题。",
                    "system_prompt": "",
                }
                await s.commit()

            # 默认 agent：端点 get-or-create 幂等（id 稳定），旧默认自动迁移。
            a1 = (await c.get("/api/v1/agents/default", headers=A)).json()
            a2 = (await c.get("/api/v1/agents/default", headers=A)).json()
            assert a1["id"] == a2["id"] and a1["is_default"] is True
            assert a1["name"] == "Zleap" and a1["avatar"] == "AI"
            async with SessionLocal() as s:
                defaults = (
                    (await s.execute(select(Agent).where(Agent.is_default.is_(True))))
                    .scalars()
                    .all()
                )
                assert len(defaults) == 1

            # 知识库 = 全部信源：新建信源后无需绑定即被 resolve
            src = (await c.post("/api/v1/sources", headers=A, json={"name": "客户端源"})).json()
            async with SessionLocal() as s:
                agent = await s.get(Agent, a1["id"])
                sources = await resolve_sources(s, agent)
                assert any(x.id == src["id"] for x in sources)

            # 上传一个文档（离线：md 解析入库）
            up = await c.post(
                f"/api/v1/sources/{src['id']}/documents",
                headers=A,
                files={"file": ("hello.md", b"# T\n\nhello sag", "text/markdown")},
            )
            assert up.status_code in (200, 201), up.text
            doc = up.json()

            # 近期动态：包含该文档；thread 建一个后也出现
            t = (await c.post(f"/api/v1/agents/{a1['id']}/threads", headers=A, json={})).json()
            acts = (await c.get("/api/v1/activity", headers=A)).json()
            assert all(x["type"] == "document" for x in acts)  # 动态=知识库事件，不含会话
            assert any(x["id"] == doc["id"] for x in acts)
            assert acts == sorted(acts, key=lambda x: x["at"], reverse=True)

            scoped_acts = (
                await c.get(
                    "/api/v1/activity",
                    headers=A,
                    params=[("source_ids", src["id"]), ("source_ids", src["id"])],
                )
            ).json()
            assert any(x["id"] == doc["id"] for x in scoped_acts)
            assert all(x["source_id"] == src["id"] for x in scoped_acts)
            empty_acts = (
                await c.get(
                    "/api/v1/activity",
                    headers=A,
                    params={"source_ids": "missing-source"},
                )
            ).json()
            assert empty_acts == []

            # 归档：PATCH → 默认列表消失、archived=true 列表出现、恢复回来
            arch = await c.patch(
                f"/api/v1/agents/{a1['id']}/threads/{t['id']}",
                headers=A,
                json={"archived": True},
            )
            assert arch.status_code == 200 and arch.json()["archived"] is True
            live = (await c.get(f"/api/v1/agents/{a1['id']}/threads", headers=A)).json()
            assert all(x["id"] != t["id"] for x in live)
            gone = (
                await c.get(f"/api/v1/agents/{a1['id']}/threads?archived=true", headers=A)
            ).json()
            assert any(x["id"] == t["id"] for x in gone)
            back = await c.patch(
                f"/api/v1/agents/{a1['id']}/threads/{t['id']}",
                headers=A,
                json={"archived": False, "title": "改名后的会话"},
            )
            assert back.json()["archived"] is False and back.json()["title"] == "改名后的会话"

            # 图片附件：上传 → 取回一致；非图片 422；纯图片也可直接发起 Agent run。
            png = bytes.fromhex(
                "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
                "0000000d4944415478da63fcffff3f030005fe02fea7568c4a0000000049454e44ae426082"
            )
            att = await c.post(
                "/api/v1/attachments", headers=A, files={"file": ("dot.png", png, "image/png")}
            )
            assert att.status_code == 201, att.text
            aid = att.json()["id"]
            got = await c.get(f"/api/v1/attachments/{aid}", headers=A)
            assert got.status_code == 200 and got.content == png
            bad = await c.post(
                "/api/v1/attachments", headers=A, files={"file": ("x.txt", b"nope", "text/plain")}
            )
            assert bad.status_code == 422
            app.state.llm = OfflineLLM()
            ask = await c.post(
                f"/api/v1/agents/{a1['id']}/threads/{t['id']}/ask",
                headers=A,
                json={"query": "", "attachments": [aid]},
            )
            assert ask.status_code == 200 and "event: run.completed" in ask.text
            msgs = (
                await c.get(f"/api/v1/agents/{a1['id']}/threads/{t['id']}/messages", headers=A)
            ).json()["items"]
            mine = [m for m in msgs if m["role"] == "user" and m["attachments"]]
            assert mine and mine[0]["attachments"][0]["id"] == aid

            # 范围问答参数被接受；消息删除端点
            scoped = await c.post(
                f"/api/v1/agents/{a1['id']}/threads/{t['id']}/ask",
                headers=A,
                json={"query": "只查这个源", "source_ids": [src["id"]]},
            )
            assert scoped.status_code == 200 and "event: run.completed" in scoped.text
            gone_id = mine[0]["id"]
            rd = await c.delete(
                f"/api/v1/agents/{a1['id']}/threads/{t['id']}/messages/{gone_id}", headers=A
            )
            assert rd.status_code == 200
            after = (
                await c.get(f"/api/v1/agents/{a1['id']}/threads/{t['id']}/messages", headers=A)
            ).json()["items"]
            assert all(m["id"] != gone_id for m in after)

            # 原文端点：200 + 内容与上传一致；不存在的 id → 404
            f = await c.get(
                f"/api/v1/sources/{src['id']}/documents/{doc['id']}/file", headers=A
            )
            assert f.status_code == 200 and b"hello sag" in f.content
            preview = await c.get(
                f"/api/v1/sources/{src['id']}/documents/{doc['id']}/preview", headers=A
            )
            assert preview.status_code == 200 and "hello sag" in preview.text
            assert preview.headers["x-muse-source-encoding"] == "utf-8"
            nf = await c.get(
                f"/api/v1/sources/{src['id']}/documents/does-not-exist/file", headers=A
            )
            assert nf.status_code == 404
