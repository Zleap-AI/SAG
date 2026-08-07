"""Regression: Message.status must roundtrip by enum *value* (lowercase).

Prior bug: SAEnum(MessageStatus, native_enum=False) 默认按 enum **name** 存（OK/FAILED/CANCELLED），
但 _ensure_columns 里的 ALTER TABLE 使用 server_default='ok'，两条写路径落库大小写不同。
存量库里凡存在 server_default 回填出的小写行，SAEnum 读回时按 name 查表就会 LookupError，
直接把 /messages 接口打成 500 → 前端整个 QA 主流程返回 "Failed to fetch"。

Fixture 里每次都是全新的 tempdir SQLite，无存量行，所以这个分歧不会自然出现；
下面的用例显式模拟"有一行是 server_default 路径写入的（小写）"这个存量场景。
"""

from __future__ import annotations

import pytest
from sqlalchemy import select, text

from sag_api.core.db import SessionLocal, init_db
from sag_api.db.models import Agent, Message, Thread
from sag_api.enums import MessageRole, MessageStatus


async def _make_thread(session) -> str:
    agent = Agent(name="enum-fixture")
    session.add(agent)
    await session.flush()
    thread = Thread(agent_id=agent.id)
    session.add(thread)
    await session.flush()
    return thread.id


@pytest.mark.asyncio
async def test_status_column_stores_enum_value_not_name():
    """ORM 写入的行落库应为小写 value（ok/failed/cancelled），与 server_default 一致。"""
    await init_db()
    async with SessionLocal() as session:
        thread_id = await _make_thread(session)
        for status in (MessageStatus.OK, MessageStatus.FAILED, MessageStatus.CANCELLED):
            session.add(
                Message(thread_id=thread_id, role=MessageRole.ASSISTANT, status=status)
            )
        await session.commit()

    async with SessionLocal() as session:
        # 绕过 ORM 直接看物理值，避免枚举适配器掩盖大小写分歧。
        rows = (await session.execute(text("SELECT status FROM messages"))).all()
    stored = {row[0] for row in rows}
    assert stored == {"ok", "failed", "cancelled"}, stored


@pytest.mark.asyncio
async def test_orm_reads_row_with_lowercase_status_value():
    """存量行的 status 若来自 server_default（小写），ORM 读回不应 LookupError。

    _ensure_columns 在既有表上 ALTER TABLE ADD COLUMN status ... DEFAULT 'ok'，
    该分支只在生产升级路径出现；下面先用 ORM 建一行合法记录，再 raw UPDATE 把 status
    强行写成小写 value，隔离出 status 列自身的 name/value 分歧行为。
    """
    await init_db()
    async with SessionLocal() as session:
        thread_id = await _make_thread(session)
        session.add(
            Message(
                id="seed-lowercase",
                thread_id=thread_id,
                role=MessageRole.ASSISTANT,
                status=MessageStatus.OK,
            )
        )
        await session.commit()

    async with SessionLocal() as session:
        # 显式覆盖成小写 value，模拟 server_default 回填 / raw SQL 迁移路径。
        await session.execute(
            text("UPDATE messages SET status='ok' WHERE id='seed-lowercase'")
        )
        await session.commit()

    async with SessionLocal() as session:
        row = (
            await session.execute(select(Message).where(Message.id == "seed-lowercase"))
        ).scalar_one()

    assert row.status is MessageStatus.OK
    assert row.status.value == "ok"


@pytest.mark.asyncio
async def test_orm_roundtrips_all_statuses_via_new_session():
    """完整来回：ORM 写 → commit → 新 session ORM 读，三个终态都不炸。"""
    await init_db()
    async with SessionLocal() as session:
        thread_id = await _make_thread(session)
        for suffix, status in [
            ("ok", MessageStatus.OK),
            ("failed", MessageStatus.FAILED),
            ("cancelled", MessageStatus.CANCELLED),
        ]:
            session.add(
                Message(
                    id=f"roundtrip-{suffix}",
                    thread_id=thread_id,
                    role=MessageRole.ASSISTANT,
                    status=status,
                )
            )
        await session.commit()

    async with SessionLocal() as session:
        loaded = (
            (
                await session.execute(
                    select(Message).where(Message.id.like("roundtrip-%")).order_by(Message.id)
                )
            )
            .scalars()
            .all()
        )
    assert {m.id: m.status for m in loaded} == {
        "roundtrip-cancelled": MessageStatus.CANCELLED,
        "roundtrip-failed": MessageStatus.FAILED,
        "roundtrip-ok": MessageStatus.OK,
    }
