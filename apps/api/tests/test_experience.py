"""R4 体验：兜底话术注入、prompt 透明、OpenAI 兼容端点。

agent-first 架构下 empty_response 不再是「跳过 LLM 的短路」，而是注入系统提示的
收尾指令；测试用桩 LLM（直答、无工具）获得确定性回答，全离线。
"""

import json

import httpx
import pytest

from sag_agent import ModelChunk


class DirectLLM:
    """直答桩：决策轮不要工具，流式输出固定 token。"""

    @property
    def configured(self):
        return True

    async def stream_turn(self, request, cancellation):
        for token in ["你好", "！"]:
            cancellation.raise_if_cancelled()
            yield ModelChunk(text_delta=token)
        yield ModelChunk(finish_reason="stop")

    async def complete(self, messages):
        return "摘要"


async def _register(c, email="exp@t.com"):
    r = await c.post("/api/v1/auth/register", json={"email": email, "password": "password123"})
    assert r.status_code == 201, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


EMPTY = "抱歉，我暂时没有查到相关资料。"


async def _make_agent_with_empty_response(c, headers):
    agent = (
        await c.post(
            "/api/v1/agents",
            headers=headers,
            json={"name": "严谨助手", "persona": {"empty_response": EMPTY}},
        )
    ).json()
    return agent


@pytest.mark.asyncio
async def test_empty_response_injection_and_prompt_preview():
    from sag_api.main import app

    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        app.state.llm = DirectLLM()
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            A = await _register(c)
            agent = await _make_agent_with_empty_response(c, A)
            thread = (await c.post(f"/api/v1/agents/{agent['id']}/threads", headers=A, json={})).json()

            events: list[tuple[str, dict]] = []
            async with c.stream(
                "POST",
                f"/api/v1/agents/{agent['id']}/threads/{thread['id']}/ask",
                headers=A,
                json={"query": "公司的报销流程是怎样的？"},
            ) as resp:
                assert resp.status_code == 200
                ev = None
                async for line in resp.aiter_lines():
                    if line.startswith("event:"):
                        ev = line.split(":", 1)[1].strip()
                    elif line.startswith("data:") and ev:
                        events.append((ev, json.loads(line.split(":", 1)[1].strip())))

            kinds = [e for e, _ in events]
            assert "message.delta" in kinds and "run.completed" in kinds
            assert "run.failed" not in kinds
            completed = next(d["payload"] for e, d in events if e == "run.completed")
            assert completed["prompt_preview"]
            assert EMPTY in completed["prompt_preview"]
            assert "公司的报销流程是怎样的？" in completed["prompt_preview"]
            assert "你好！" not in completed["prompt_preview"]
            tokens = "".join(d["payload"]["delta"] for e, d in events if e == "message.delta")
            assert tokens == "你好！"

            # 该轮回答已落库
            msgs = (await c.get(f"/api/v1/agents/{agent['id']}/threads/{thread['id']}/messages", headers=A)).json()[
                "items"
            ]
            saved = next(m for m in msgs if m["content"] == "你好！" and m["role"] == "assistant")
            assert saved["prompt_preview"] == completed["prompt_preview"]
            assert saved["content"] not in saved["prompt_preview"]


@pytest.mark.asyncio
async def test_openai_compatible_endpoint():
    from sag_api.main import app

    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        app.state.llm = DirectLLM()
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            A = await _register(c, "oai@t.com")
            agent = await _make_agent_with_empty_response(c, A)

            # 非流式：标准 OpenAI ChatCompletion 结构
            r = await c.post(
                f"/api/v1/openai/{agent['id']}/chat/completions",
                headers=A,
                json={"messages": [{"role": "user", "content": "介绍一下产品定价"}]},
            )
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["object"] == "chat.completion"
            assert body["choices"][0]["message"]["content"] == "你好！"
            assert body["choices"][0]["finish_reason"] == "stop"
            assert "sag" in body  # 扩展字段：引用

            # 流式：SSE chunk + [DONE]
            chunks: list[str] = []
            async with c.stream(
                "POST",
                f"/api/v1/openai/{agent['id']}/chat/completions",
                headers=A,
                json={"messages": [{"role": "user", "content": "你好"}], "stream": True},
            ) as resp:
                assert resp.status_code == 200
                async for line in resp.aiter_lines():
                    if line.startswith("data:"):
                        chunks.append(line[len("data:") :].strip())
            assert chunks[-1] == "[DONE]"
            content = "".join(
                json.loads(ch)["choices"][0]["delta"].get("content", "") for ch in chunks if ch != "[DONE]"
            )
            assert content == "你好！"

            # 缺少 user 消息 → 422
            bad = await c.post(
                f"/api/v1/openai/{agent['id']}/chat/completions",
                headers=A,
                json={"messages": [{"role": "system", "content": "x"}]},
            )
            assert bad.status_code == 422

            # 未认证 → 401
            un = await c.post(
                f"/api/v1/openai/{agent['id']}/chat/completions",
                json={"messages": [{"role": "user", "content": "hi"}]},
            )
            assert un.status_code == 401
