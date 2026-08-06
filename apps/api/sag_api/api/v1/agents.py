from __future__ import annotations

import json
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sse_starlette.sse import EventSourceResponse

from sag_agent import AgentRuntime, EventType, RunHandle
from sag_api.core.db import SessionLocal, get_session
from sag_api.core.deps import (
    get_agent_runtime,
    get_current_user,
    get_engine_manager,
    get_llm,
    get_tool_registry,
)
from sag_api.core.error_taxonomy import ErrorCode
from sag_api.core.errors import ConfigurationError, ConflictError, NotFoundError
from sag_api.core.logging import get_logger
from sag_api.db.models import User
from sag_api.generation import LLMClient
from sag_api.sag import EngineManager
from sag_api.schemas.agent import (
    AgentCreate,
    AgentOut,
    AgentUpdate,
    AskRequest,
    BindingCreate,
    BindingOut,
    MessageOut,
    MessagePageOut,
    ThreadCreate,
    ThreadOut,
    ThreadUpdate,
    ToolRejection,
)
from sag_api.schemas.common import Ok
from sag_api.services import agent_domain as svc
from sag_api.services import agent_service
from sag_api.tools import ToolRegistry

router = APIRouter(prefix="/agents", tags=["agents"])
log = get_logger("agents")


def _sse(event: str, payload: dict) -> dict:
    return {"event": event, "data": json.dumps(payload, ensure_ascii=False)}


async def _owned_run(
    session: AsyncSession,
    runtime: AgentRuntime,
    *,
    agent_id: str,
    thread_id: str,
    run_id: str,
) -> RunHandle:
    agent = await svc.get_agent(session, agent_id)
    await svc.get_thread(session, agent.id, thread_id)
    handle = runtime.get_run(run_id)
    metadata = handle.context.metadata if handle is not None else {}
    if (
        handle is None
        or metadata.get("agent_id") != agent.id
        or metadata.get("thread_id") != thread_id
    ):
        raise NotFoundError("运行不存在或已结束")
    return handle


# ── CRUD ────────────────────────────────────────────────────────────
@router.get("", response_model=list[AgentOut])
async def list_(
    _user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)
):
    return [AgentOut.model_validate(a) for a in await svc.list_agents(session)]


@router.post("", response_model=AgentOut, status_code=201)
async def create(
    body: AgentCreate,
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    agent = await svc.create_agent(session, name=body.name, avatar=body.avatar, persona=body.persona)
    return AgentOut.model_validate(agent)


@router.get("/default", response_model=AgentOut)
async def get_default(
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """默认 agent（get-or-create）：客户端主对话入口，知识库=全部信源。"""
    return AgentOut.model_validate(await svc.get_default_agent(session))


@router.get("/{agent_id}", response_model=AgentOut)
async def get_(
    agent_id: str,
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    return AgentOut.model_validate(await svc.get_agent(session, agent_id))


@router.patch("/{agent_id}", response_model=AgentOut)
async def update_(
    agent_id: str,
    body: AgentUpdate,
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    agent = await svc.update_agent(
        session, agent_id, name=body.name, avatar=body.avatar, persona=body.persona
    )
    return AgentOut.model_validate(agent)


@router.delete("/{agent_id}", response_model=Ok)
async def delete_(
    agent_id: str,
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    await svc.delete_agent(session, agent_id)
    return Ok(detail="Agent 已删除")


# ── 绑定（信源 / MCP）────────────────────────────────────────────────
@router.get("/{agent_id}/bindings", response_model=list[BindingOut])
async def list_bindings(
    agent_id: str,
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    agent = await svc.get_agent(session, agent_id)
    return [BindingOut.model_validate(b) for b in await svc.list_bindings(session, agent)]


@router.post("/{agent_id}/bindings", response_model=BindingOut, status_code=201)
async def add_binding(
    agent_id: str,
    body: BindingCreate,
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    agent = await svc.get_agent(session, agent_id)
    b = await svc.add_binding(
        session, agent, target_type=body.target_type, target_id=body.target_id, config=body.config
    )
    return BindingOut.model_validate(b)


@router.delete("/{agent_id}/bindings/{binding_id}", response_model=Ok)
async def remove_binding(
    agent_id: str,
    binding_id: str,
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    agent = await svc.get_agent(session, agent_id)
    await svc.remove_binding(session, agent, binding_id)
    return Ok(detail="已解绑")


# ── 会话 ────────────────────────────────────────────────────────────
@router.get("/{agent_id}/threads", response_model=list[ThreadOut])
async def list_threads(
    agent_id: str,
    archived: bool = False,
    limit: int = Query(
        default=svc.THREAD_PAGE_DEFAULT,
        ge=1,
        le=svc.THREAD_PAGE_MAX,
    ),
    offset: int = Query(default=0, ge=0),
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    agent = await svc.get_agent(session, agent_id)
    return [
        ThreadOut.model_validate(t)
        for t in await svc.list_threads(
            session,
            agent.id,
            archived=archived,
            limit=limit,
            offset=offset,
        )
    ]


@router.post("/{agent_id}/threads", response_model=ThreadOut, status_code=201)
async def create_thread(
    agent_id: str,
    body: ThreadCreate,
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    agent = await svc.get_agent(session, agent_id)
    thread = await svc.create_thread(session, agent, body.title)
    return ThreadOut.model_validate(thread)


@router.patch("/{agent_id}/threads/{thread_id}", response_model=ThreadOut)
async def update_thread(
    agent_id: str,
    thread_id: str,
    body: ThreadUpdate,
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    agent = await svc.get_agent(session, agent_id)
    t = await svc.update_thread(
        session, agent.id, thread_id, title=body.title, archived=body.archived
    )
    return ThreadOut.model_validate(t)


@router.get("/{agent_id}/threads/{thread_id}/messages", response_model=MessagePageOut)
async def messages(
    agent_id: str,
    thread_id: str,
    limit: int = Query(default=svc.MESSAGE_PAGE_DEFAULT, ge=1, le=svc.MESSAGE_PAGE_MAX),
    cursor: str | None = Query(default=None, max_length=svc.MESSAGE_CURSOR_MAX_LENGTH),
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    agent = await svc.get_agent(session, agent_id)
    thread = await svc.get_thread(session, agent.id, thread_id)
    page = await svc.list_messages_page(session, thread.id, limit=limit, cursor=cursor)
    return MessagePageOut(
        items=[MessageOut.model_validate(message) for message in page.items],
        next_cursor=page.next_cursor,
        has_more=page.has_more,
    )


@router.delete("/{agent_id}/threads/{thread_id}", response_model=Ok)
async def delete_thread(
    agent_id: str,
    thread_id: str,
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    agent = await svc.get_agent(session, agent_id)
    await svc.delete_thread(session, agent.id, thread_id)
    return Ok(detail="会话已删除")


@router.delete("/{agent_id}/threads/{thread_id}/messages/{message_id}", response_model=Ok)
async def delete_message(
    agent_id: str,
    thread_id: str,
    message_id: str,
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    agent = await svc.get_agent(session, agent_id)
    await svc.delete_message(session, agent.id, thread_id, message_id)
    return Ok(detail="已删除")


@router.post("/{agent_id}/threads/{thread_id}/runs/{run_id}/cancel", response_model=Ok)
async def cancel_run(
    agent_id: str,
    thread_id: str,
    run_id: str,
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    agent_runtime: AgentRuntime = Depends(get_agent_runtime),
):
    handle = await _owned_run(
        session,
        agent_runtime,
        agent_id=agent_id,
        thread_id=thread_id,
        run_id=run_id,
    )
    handle.cancel()
    return Ok(detail="已停止")


@router.post(
    "/{agent_id}/threads/{thread_id}/runs/{run_id}/tool-calls/{tool_call_id}/approve",
    response_model=Ok,
)
async def approve_tool_call(
    agent_id: str,
    thread_id: str,
    run_id: str,
    tool_call_id: str,
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    agent_runtime: AgentRuntime = Depends(get_agent_runtime),
):
    handle = await _owned_run(
        session,
        agent_runtime,
        agent_id=agent_id,
        thread_id=thread_id,
        run_id=run_id,
    )
    if not handle.approve(tool_call_id):
        raise ConflictError("工具调用当前不等待审批")
    return Ok(detail="已允许执行")


@router.post(
    "/{agent_id}/threads/{thread_id}/runs/{run_id}/tool-calls/{tool_call_id}/reject",
    response_model=Ok,
)
async def reject_tool_call(
    agent_id: str,
    thread_id: str,
    run_id: str,
    tool_call_id: str,
    body: ToolRejection,
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    agent_runtime: AgentRuntime = Depends(get_agent_runtime),
):
    handle = await _owned_run(
        session,
        agent_runtime,
        agent_id=agent_id,
        thread_id=thread_id,
        run_id=run_id,
    )
    if not handle.reject(tool_call_id, body.reason):
        raise ConflictError("工具调用当前不等待审批")
    return Ok(detail="已拒绝执行")


@router.post("/{agent_id}/threads/{thread_id}/ask")
async def ask(
    agent_id: str,
    thread_id: str,
    body: AskRequest,
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    engine_manager: EngineManager = Depends(get_engine_manager),
    llm: LLMClient = Depends(get_llm),
    tool_registry: ToolRegistry = Depends(get_tool_registry),
    agent_runtime: AgentRuntime = Depends(get_agent_runtime),
) -> EventSourceResponse:
    agent = await svc.get_agent(session, agent_id)
    thread = await svc.get_thread(session, agent.id, thread_id)
    if not llm.configured:
        raise ConfigurationError("尚未配置 LLM，无法生成回答")
    plan = await svc.prepare_ask(
        session,
        agent=agent,
        thread=thread,
        query=body.query,
        attachments=body.attachments,
        source_ids=body.source_ids,
        llm=llm,
    )
    web_enabled = body.effective_web_enabled

    async def event_gen():
        last = None
        try:
            async for event in agent_service.generate_stream(
                SessionLocal,
                plan=plan,
                agent=agent,
                thread_id=thread.id,
                engine_manager=engine_manager,
                llm=llm,
                tool_registry=tool_registry,
                runtime=agent_runtime,
                knowledge_only=not web_enabled,
            ):
                last = event
                yield _sse(event.type, event.data)
        except Exception as e:  # noqa: BLE001
            log.exception("ask 流异常终止：%s", e)
            run_id = last.data.get("run_id", "") if last is not None else ""
            sequence = int(last.data.get("sequence", 0)) + 1 if last is not None else 0
            data = {
                "version": 1,
                "type": EventType.RUN_FAILED.value,
                "run_id": run_id,
                "sequence": sequence,
                "timestamp": datetime.now(UTC).isoformat(),
                "turn": 0,
                "payload": {
                    "error": {
                        "code": ErrorCode.STREAM_ERROR,
                        "message": f"生成中断：{getattr(e, 'message', None) or e}",
                        "retryable": True,
                        "details": {},
                    }
                },
            }
            yield _sse(
                EventType.RUN_FAILED.value,
                data,
            )

    return EventSourceResponse(
        event_gen(),
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
