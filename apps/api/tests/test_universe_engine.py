"""Exercise aggregate overview and keyset expansion against the real graph store."""

import asyncio
import uuid
from datetime import datetime, timedelta

import httpx
import pytest
from sqlalchemy import delete, select


def test_universe_cursor_protocol_rejects_v1_tokens():
    from sag_api.sag.engine_manager import (
        _decode_universe_cursor,
        _encode_universe_cursor,
    )

    legacy = _encode_universe_cursor({"v": 1}, "test-secret")
    with pytest.raises(ValueError, match="invalid universe cursor"):
        _decode_universe_cursor(legacy, "test-secret")


@pytest.mark.asyncio
async def test_universe_real_store_statistics_and_keyset_cursor():
    from sag_api.core.db import SessionLocal
    from sag_api.db.models import Source, UniverseDirtySource, User
    from sag_api.main import app

    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            email = f"universe-engine-{uuid.uuid4().hex}@t.com"
            token = (
                await client.post(
                    "/api/v1/auth/register",
                    json={
                        "email": email,
                        "password": "password123",
                    },
                )
            ).json()["access_token"]
            headers = {"Authorization": f"Bearer {token}"}
            source_body = (
                await client.post(
                    "/api/v1/sources",
                    headers=headers,
                    json={"name": "时序图谱测试源"},
                )
            ).json()
            source_id = source_body["id"]
            async with SessionLocal() as session:
                source = await session.get(Source, source_id)
                assert source is not None
                source_config_id = source.sag_source_config_id
                user_id = await session.scalar(select(User.id).where(User.email == email))
                assert user_id is not None

            from zleap.sag.db import get_session_factory
            from zleap.sag.db.models import (
                Entity,
                EntityType,
                EventEntity,
                SourceConfig,
                SourceEvent,
            )

            entity_id = uuid.uuid4().hex
            auxiliary_entity_ids: list[str] = []
            entity_type_id = uuid.uuid4().hex
            event_ids: list[str] = []
            old_event_id = uuid.uuid4().hex
            foreign_entity_id = uuid.uuid4().hex
            foreign_source_config_id = f"src_{uuid.uuid4().hex}"
            base_time = datetime.now() - timedelta(days=1)
            session_factory = get_session_factory()
            async with session_factory() as session:
                await session.merge(SourceConfig(id=source_config_id, name="时序图谱测试源"))
                await session.merge(
                    SourceConfig(id=foreign_source_config_id, name="异常跨源引用")
                )
                session.add(
                    EntityType(
                        id=entity_type_id,
                        type=f"concept_{entity_type_id[:8]}",
                        name="概念",
                    )
                )
                session.add(
                    Entity(
                        id=entity_id,
                        source_config_id=source_config_id,
                        entity_type_id=entity_type_id,
                        type="concept",
                        name="SAG 动态图谱",
                        normalized_name="sag动态图谱",
                        description="有界、按需生长的知识图谱",
                    )
                )
                await session.flush()
                for index in range(23):
                    event_id = uuid.uuid4().hex
                    event_ids.append(event_id)
                    # 每三条共享同一时间，验证复合游标的稳定 tie-break。
                    event_time = base_time - timedelta(days=index // 3)
                    session.add(
                        SourceEvent(
                            id=event_id,
                            source_config_id=source_config_id,
                            source_type="doc",
                            source_id="timeline-doc",
                            title=f"时序事件 {index:02d}",
                            summary="按时间和 ID 稳定分页。",
                            content="测试内容",
                            category="时序测试",
                            chunk_id=f"chunk-{index}",
                            start_time=event_time,
                            created_time=event_time,
                        )
                    )
                    session.add(
                        EventEntity(
                            id=uuid.uuid4().hex,
                            event_id=event_id,
                            entity_id=entity_id,
                            weight=1.0,
                        )
                    )
                for index in range(10):
                    auxiliary_id = uuid.uuid4().hex
                    auxiliary_entity_ids.append(auxiliary_id)
                    session.add(
                        Entity(
                            id=auxiliary_id,
                            source_config_id=source_config_id,
                            entity_type_id=entity_type_id,
                            type="concept",
                            name=f"关联主题 {index:02d}",
                            normalized_name=f"关联主题{index:02d}",
                            description="用于验证实体优先的时间分页。",
                        )
                    )
                    session.add(
                        EventEntity(
                            id=uuid.uuid4().hex,
                            event_id=event_ids[1],
                            entity_id=auxiliary_id,
                            weight=0.5,
                        )
                    )
                session.add(
                    Entity(
                        id=foreign_entity_id,
                        source_config_id=foreign_source_config_id,
                        entity_type_id=entity_type_id,
                        type="concept",
                        name="跨源实体",
                        normalized_name="跨源实体",
                    )
                )
                session.add(
                    EventEntity(
                        id=uuid.uuid4().hex,
                        event_id=event_ids[1],
                        entity_id=foreign_entity_id,
                        weight=2.0,
                    )
                )
                old_event_time = base_time - timedelta(days=500)
                session.add(
                    SourceEvent(
                        id=old_event_id,
                        source_config_id=source_config_id,
                        source_type="doc",
                        source_id="timeline-doc",
                        title="较早的历史事件",
                        summary="应通过实体关系的游标分页继续抵达。",
                        content="测试内容",
                        category="时序测试",
                        chunk_id="chunk-old",
                        start_time=old_event_time,
                        created_time=old_event_time,
                    )
                )
                session.add(
                    EventEntity(
                        id=uuid.uuid4().hex,
                        event_id=old_event_id,
                        entity_id=entity_id,
                        weight=1.0,
                    )
                )
                await session.commit()

            rebuilt = await client.post("/api/v1/universe/rebuild", headers=headers)
            assert rebuilt.status_code == 202, rebuilt.text
            for _ in range(500):
                job_response = await client.get(
                    f"/api/v1/jobs/{rebuilt.json()['id']}", headers=headers
                )
                assert job_response.status_code == 200, job_response.text
                if job_response.json()["status"] in {"succeeded", "failed"}:
                    break
                await asyncio.sleep(0.01)
            assert job_response.json()["status"] == "succeeded", job_response.text
            rebuilt = await client.get("/api/v1/universe/manifest", headers=headers)
            assert rebuilt.status_code == 200, rebuilt.text
            partition = next(
                item
                for item in rebuilt.json()["partitions"]
                if item["kind"] == "source" and item["source_id"] == source_id
            )
            assert partition["event_count"] == 24
            assert partition["entity_count"] == 11
            assert partition["relation_count"] == 34
            assert sum(bucket["count"] for bucket in partition["time_buckets"]) == 24
            assert rebuilt.json()["policy"]["timeline_event_page_size"] == 20
            assert rebuilt.json()["policy"]["event_entity_limit"] == 8

            oversized_timeline = await client.post(
                "/api/v1/universe/timeline",
                headers=headers,
                json={
                    "epoch": 10,
                    "source_id": source_id,
                    "limit": 51,
                },
            )
            assert oversized_timeline.status_code == 422

            async def timeline(
                cursor: str | None = None,
                snapshot_id: str | None = None,
                direction: str = "older",
            ):
                response = await client.post(
                    "/api/v1/universe/timeline",
                    headers=headers,
                    json={
                        "epoch": 10,
                        "source_id": source_id,
                        "limit": 6,
                        "direction": direction,
                        "cursor": cursor,
                        "snapshot_id": snapshot_id,
                    },
                )
                assert response.status_code == 200, response.text
                return response.json()

            timeline_pages = [await timeline()]
            retried_first_timeline_page = await timeline(
                snapshot_id=timeline_pages[0]["snapshot_id"]
            )
            assert retried_first_timeline_page["page_id"] == timeline_pages[0]["page_id"]
            assert retried_first_timeline_page["bundles"] == timeline_pages[0]["bundles"]
            missing_snapshot = await client.post(
                "/api/v1/universe/timeline",
                headers=headers,
                json={
                    "epoch": 10,
                    "source_id": source_id,
                    "limit": 6,
                    "cursor": timeline_pages[0]["page"]["next_cursor"],
                },
            )
            assert missing_snapshot.status_code == 422
            while timeline_pages[-1]["page"]["has_more"]:
                timeline_pages.append(
                    await timeline(
                        timeline_pages[-1]["page"]["next_cursor"],
                        timeline_pages[0]["snapshot_id"],
                    )
                )
            assert [
                page["page"]["returned_bundles"] for page in timeline_pages
            ] == [6, 6, 6, 6]
            assert timeline_pages[0]["page"]["has_newer"] is False
            assert timeline_pages[0]["page"]["newer_cursor"] is None
            assert timeline_pages[-1]["page"]["has_older"] is False
            assert timeline_pages[-1]["page"]["older_cursor"] is None

            recovered_newer_page = await timeline(
                timeline_pages[1]["page"]["newer_cursor"],
                timeline_pages[0]["snapshot_id"],
                "newer",
            )
            assert recovered_newer_page["request_direction"] == "newer"
            assert recovered_newer_page["page"]["direction"] == "newer"
            assert recovered_newer_page["page"]["has_newer"] is False
            assert recovered_newer_page["page"]["has_older"] is True
            assert [
                bundle["event"]["id"] for bundle in recovered_newer_page["bundles"]
            ] == [
                bundle["event"]["id"] for bundle in timeline_pages[0]["bundles"]
            ]
            timeline_events = [
                bundle["event"]["id"]
                for page in timeline_pages
                for bundle in page["bundles"]
            ]
            assert len(timeline_events) == len(set(timeline_events)) == 24
            assert set(timeline_events) == {*event_ids, old_event_id}
            assert timeline_events[-1] == old_event_id

            # The counting axis: ordinals stay contiguous across page seams and
            # every page reports the same snapshot event total.
            assert [
                bundle["ordinal"]
                for page in timeline_pages
                for bundle in page["bundles"]
            ] == list(range(24))
            assert all(page["total_events"] == 24 for page in timeline_pages)
            assert recovered_newer_page["total_events"] == 24
            assert [
                bundle["ordinal"] for bundle in recovered_newer_page["bundles"]
            ] == [0, 1, 2, 3, 4, 5]
            assert sum(
                len(bundle["relations"])
                for page in timeline_pages
                for bundle in page["bundles"]
            ) == 31
            assert max(
                bundle["event"]["related_count"]
                for page in timeline_pages
                for bundle in page["bundles"]
            ) == 11
            for page in timeline_pages:
                assert page["schema_version"] == 3
                assert "nodes" not in page
                assert "relations" not in page
                assert len(page["bundles"]) == page["page"]["returned_bundles"]
                assert page["bundles"][0]["cursor_before"] == page["page"][
                    "newer_cursor"
                ]
                assert page["bundles"][-1]["cursor_after"] == page["page"]["next_cursor"]
                event_ids_in_page = {
                    bundle["event"]["id"] for bundle in page["bundles"]
                }
                entity_ids_in_page = {
                    node["id"]
                    for bundle in page["bundles"]
                    for node in bundle["nodes"]
                }
                page_relations = [
                    relation
                    for bundle in page["bundles"]
                    for relation in bundle["relations"]
                ]
                assert all(
                    relation["from_id"] in event_ids_in_page
                    for relation in page_relations
                )
                assert all(
                    relation["to_id"] in entity_ids_in_page
                    for relation in page_relations
                )
                event_counts = {
                    bundle["event"]["id"]: bundle["event"]["related_count"]
                    for bundle in page["bundles"]
                }
                assert all(
                    sum(
                        1
                        for relation in page_relations
                        if relation["from_id"] == event_id
                    )
                    == min(event_counts[event_id], 8)
                    for event_id in event_ids_in_page
                )
                assert page["page"]["returned_relations"] == len(page_relations)
                assert page["page"]["returned_unique_nodes"] == (
                    len(event_ids_in_page) + len(entity_ids_in_page)
                )
                for bundle in page["bundles"]:
                    assert bundle["event"]["id"] in event_ids_in_page
                    assert bundle["neighbor_page"]["returned_unique"] == len(
                        bundle["nodes"]
                    )
                    assert bundle["neighbor_page"]["complete"] == (
                        bundle["neighbor_page"]["returned_unique"]
                        >= bundle["neighbor_page"]["total_unique"]
                    )
                    assert bundle["neighbor_page"]["complete"] == (
                        bundle["neighbor_page"]["next_cursor"] is None
                    )
                    assert all(
                        relation["from_id"] == bundle["event"]["id"]
                        for relation in bundle["relations"]
                    )

            first_page_event_ids = [
                bundle["event"]["id"] for bundle in timeline_pages[0]["bundles"]
            ]
            resumed_mid_page = await timeline(
                timeline_pages[0]["bundles"][2]["cursor_after"],
                timeline_pages[0]["snapshot_id"],
            )
            assert resumed_mid_page["request_cursor"] == timeline_pages[0]["bundles"][2][
                "cursor_after"
            ]
            assert resumed_mid_page["bundles"][0]["event"]["id"] == first_page_event_ids[3]

            partial_bundle = next(
                bundle
                for page in timeline_pages
                for bundle in page["bundles"]
                if bundle["event"]["id"] == event_ids[1]
            )
            assert partial_bundle["neighbor_page"]["returned_unique"] == 8
            assert partial_bundle["neighbor_page"]["total_unique"] == 11
            assert partial_bundle["neighbor_page"]["complete"] is False
            assert partial_bundle["neighbor_page"]["next_cursor"]
            event_neighbor_remainder = await client.post(
                "/api/v1/universe/expand",
                headers=headers,
                json={
                    "epoch": 11,
                    "source_id": source_id,
                    "node_kind": "event",
                    "node_id": event_ids[1],
                    "limit": 8,
                    "cursor": partial_bundle["neighbor_page"]["next_cursor"],
                    "snapshot_id": timeline_pages[0]["snapshot_id"],
                },
            )
            assert event_neighbor_remainder.status_code == 200, (
                event_neighbor_remainder.text
            )
            remainder_patch = event_neighbor_remainder.json()
            assert remainder_patch["schema_version"] == 2
            assert remainder_patch["snapshot_id"] == timeline_pages[0]["snapshot_id"]
            assert remainder_patch["page"]["returned"] == 3
            assert remainder_patch["page"]["has_more"] is False
            assert {
                node["id"] for node in remainder_patch["nodes"]
            }.isdisjoint(
                {node["id"] for node in partial_bundle["nodes"]}
            )

            late_event_id = uuid.uuid4().hex
            late_relation_id = uuid.uuid4().hex
            snapshot_time = datetime.fromisoformat(timeline_pages[0]["as_of"])
            snapshot_time_db = snapshot_time.replace(tzinfo=None)
            async with session_factory() as session:
                session.add(
                    SourceEvent(
                        id=late_event_id,
                        source_config_id=source_config_id,
                        source_type="doc",
                        source_id="late-doc",
                        title="快照后迟到的历史事件",
                        summary="开始时间虽早，写入时间晚于快照。",
                        content="测试内容",
                        category="时序测试",
                        start_time=snapshot_time_db - timedelta(seconds=1),
                        created_time=snapshot_time_db + timedelta(seconds=1),
                    )
                )
                session.add(
                    EventEntity(
                        id=late_relation_id,
                        event_id=late_event_id,
                        entity_id=entity_id,
                        weight=3.0,
                        created_time=snapshot_time_db + timedelta(seconds=1),
                    )
                )
                await session.commit()

            stable_timeline_root = await timeline(
                snapshot_id=timeline_pages[0]["snapshot_id"]
            )
            assert stable_timeline_root["page_id"] == timeline_pages[0]["page_id"]
            assert all(
                bundle["event"]["id"] != late_event_id
                for bundle in stable_timeline_root["bundles"]
            )
            stable_entity_root = await client.post(
                "/api/v1/universe/expand",
                headers=headers,
                json={
                    "epoch": 11,
                    "source_id": source_id,
                    "node_kind": "entity",
                    "node_id": entity_id,
                    "limit": 4,
                    "snapshot_id": timeline_pages[0]["snapshot_id"],
                },
            )
            assert stable_entity_root.status_code == 200, stable_entity_root.text
            assert all(
                node["id"] != late_event_id
                for node in stable_entity_root.json()["nodes"]
            )
            hidden_late_anchor = await client.post(
                "/api/v1/universe/expand",
                headers=headers,
                json={
                    "epoch": 11,
                    "source_id": source_id,
                    "node_kind": "event",
                    "node_id": late_event_id,
                    "limit": 8,
                    "snapshot_id": timeline_pages[0]["snapshot_id"],
                },
            )
            assert hidden_late_anchor.status_code == 404
            async with session_factory() as session:
                await session.execute(
                    delete(EventEntity).where(EventEntity.id == late_relation_id)
                )
                await session.execute(
                    delete(SourceEvent).where(SourceEvent.id == late_event_id)
                )
                await session.commit()

            async with SessionLocal() as session:
                session.add(
                    UniverseDirtySource(
                        user_id=user_id,
                        source_id=source_id,
                        reason="timeline-revision-test",
                        revision=1,
                    )
                )
                await session.commit()
            stale_snapshot = await client.post(
                "/api/v1/universe/timeline",
                headers=headers,
                json={
                    "epoch": 10,
                    "source_id": source_id,
                    "limit": 6,
                    "cursor": timeline_pages[0]["page"]["next_cursor"],
                    "snapshot_id": timeline_pages[0]["snapshot_id"],
                },
            )
            assert stale_snapshot.status_code == 409
            assert stale_snapshot.json()["error"]["code"] == "snapshot_changed"
            async with SessionLocal() as session:
                await session.execute(
                    delete(UniverseDirtySource).where(
                        UniverseDirtySource.user_id == user_id,
                        UniverseDirtySource.source_id == source_id,
                    )
                )
                await session.commit()

            async def expand(
                cursor: str | None = None,
                snapshot_id: str | None = None,
                node_id: str = entity_id,
            ):
                response = await client.post(
                    "/api/v1/universe/expand",
                    headers=headers,
                    json={
                        "epoch": 11,
                        "source_id": source_id,
                        "node_kind": "entity",
                        "node_id": node_id,
                        "limit": 4,
                        "cursor": cursor,
                        "snapshot_id": snapshot_id,
                    },
                )
                assert response.status_code == 200, response.text
                return response.json()

            pages = [await expand()]
            assert pages[0]["anchor"]["related_count"] == 24

            oversized_entity_page = await client.post(
                "/api/v1/universe/expand",
                headers=headers,
                json={
                    "epoch": 11,
                    "source_id": source_id,
                    "node_kind": "entity",
                    "node_id": entity_id,
                    "limit": 5,
                },
            )
            assert oversized_entity_page.status_code == 422

            while pages[-1]["page"]["has_more"]:
                pages.append(
                    await expand(
                        pages[-1]["page"]["next_cursor"],
                        pages[0]["snapshot_id"],
                    )
                )
            assert [page["page"]["returned"] for page in pages] == [4] * 6
            assert pages[-1]["page"]["next_cursor"] is None
            paged_ids = [
                node["id"]
                for page in pages
                for node in page["nodes"]
                if node["kind"] == "event"
            ]
            assert len(paged_ids) == len(set(paged_ids)) == 24
            assert set(paged_ids) == {*event_ids, old_event_id}
            projected_entity_ids = {
                node["id"]
                for page in pages
                for node in page["nodes"]
                if node["kind"] == "entity"
            }
            assert len(set(auxiliary_entity_ids) & projected_entity_ids) == 7
            for page in pages:
                assert page["schema_version"] == 2
                assert page["source_id"] == source_id
                assert page["snapshot_id"] == pages[0]["snapshot_id"]
                assert page["bundle_id"].endswith(page["page_id"])
                page_event_ids = {
                    node["id"] for node in page["nodes"] if node["kind"] == "event"
                }
                page_entity_ids = {
                    node["id"] for node in page["nodes"] if node["kind"] == "entity"
                } | {entity_id}
                assert all(
                    relation["from_id"] in page_event_ids
                    and relation["to_id"] in page_entity_ids
                    for relation in page["relations"]
                )

            wrong_anchor = await client.post(
                "/api/v1/universe/expand",
                headers=headers,
                json={
                    "epoch": 11,
                    "source_id": source_id,
                    "node_kind": "entity",
                    "node_id": uuid.uuid4().hex,
                    "limit": 1,
                    "cursor": pages[0]["page"]["next_cursor"],
                    "snapshot_id": pages[0]["snapshot_id"],
                },
            )
            assert wrong_anchor.status_code == 422

            cursor = pages[0]["page"]["next_cursor"]
            assert cursor
            missing_expand_snapshot = await client.post(
                "/api/v1/universe/expand",
                headers=headers,
                json={
                    "epoch": 11,
                    "source_id": source_id,
                    "node_kind": "entity",
                    "node_id": entity_id,
                    "limit": 4,
                    "cursor": cursor,
                },
            )
            assert missing_expand_snapshot.status_code == 422
            tampered = f"{cursor[:-1]}{'A' if cursor[-1] != 'A' else 'B'}"
            tampered_response = await client.post(
                "/api/v1/universe/expand",
                headers=headers,
                json={
                    "epoch": 11,
                    "source_id": source_id,
                    "node_kind": "entity",
                    "node_id": entity_id,
                    "limit": 4,
                    "cursor": tampered,
                    "snapshot_id": pages[0]["snapshot_id"],
                },
            )
            assert tampered_response.status_code == 422

            invalid_window = await client.post(
                "/api/v1/universe/expand",
                headers=headers,
                json={
                    "epoch": 11,
                    "source_id": source_id,
                    "node_kind": "entity",
                    "node_id": entity_id,
                    "after": "2026-07-12T00:00:00Z",
                    "before": "2026-01-01T00:00:00Z",
                },
            )
            assert invalid_window.status_code == 422

            event_expand = await client.post(
                "/api/v1/universe/expand",
                headers=headers,
                json={
                    "epoch": 11,
                    "source_id": source_id,
                    "node_kind": "event",
                    "node_id": event_ids[0],
                    "limit": 1,
                    "snapshot_id": timeline_pages[0]["snapshot_id"],
                },
            )
            assert event_expand.status_code == 200, event_expand.text
            assert event_expand.json()["snapshot_id"] == timeline_pages[0]["snapshot_id"]
            assert event_expand.json()["anchor"]["related_count"] == 1
            assert event_expand.json()["nodes"][0]["id"] == entity_id
            assert event_expand.json()["page"]["has_more"] is False

            async with SessionLocal() as session:
                session.add(
                    UniverseDirtySource(
                        user_id=user_id,
                        source_id=source_id,
                        reason="expand-revision-test",
                        revision=2,
                    )
                )
                await session.commit()
            stale_root_expand = await client.post(
                "/api/v1/universe/expand",
                headers=headers,
                json={
                    "epoch": 11,
                    "source_id": source_id,
                    "node_kind": "entity",
                    "node_id": entity_id,
                    "limit": 4,
                    "snapshot_id": pages[0]["snapshot_id"],
                },
            )
            assert stale_root_expand.status_code == 409
            assert stale_root_expand.json()["error"]["code"] == "snapshot_changed"
            stale_continuation_expand = await client.post(
                "/api/v1/universe/expand",
                headers=headers,
                json={
                    "epoch": 11,
                    "source_id": source_id,
                    "node_kind": "entity",
                    "node_id": entity_id,
                    "limit": 4,
                    "cursor": cursor,
                    "snapshot_id": pages[0]["snapshot_id"],
                },
            )
            assert stale_continuation_expand.status_code == 409
            assert stale_continuation_expand.json()["error"]["code"] == (
                "snapshot_changed"
            )


@pytest.mark.asyncio
async def test_universe_timeline_orders_same_instant_book_by_narrative_rank():
    """An imported book stamps every event with one instant; the canonical
    exploration order must fall back to the extractor's narrative rank, and the
    ordinals must stay contiguous so the client's counting axis can carry it."""
    from sag_api.core.db import SessionLocal
    from sag_api.db.models import Source
    from sag_api.main import app

    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            email = f"universe-book-{uuid.uuid4().hex}@t.com"
            token = (
                await client.post(
                    "/api/v1/auth/register",
                    json={"email": email, "password": "password123"},
                )
            ).json()["access_token"]
            headers = {"Authorization": f"Bearer {token}"}
            source_body = (
                await client.post(
                    "/api/v1/sources",
                    headers=headers,
                    json={"name": "同刻书籍源"},
                )
            ).json()
            source_id = source_body["id"]
            async with SessionLocal() as session:
                source = await session.get(Source, source_id)
                assert source is not None
                source_config_id = source.sag_source_config_id

            from zleap.sag.db import get_session_factory
            from zleap.sag.db.models import (
                Entity,
                EntityType,
                EventEntity,
                SourceConfig,
                SourceEvent,
            )

            imported_at = datetime.now() - timedelta(days=2)
            entity_type_id = uuid.uuid4().hex
            entity_id = uuid.uuid4().hex
            # Ids sort in the exact opposite direction of ranks, so any id
            # tie-break would reverse the book; only rank produces reading order.
            chapter_ids = [f"{9 - rank}{uuid.uuid4().hex[:12]}" for rank in range(8)]
            session_factory = get_session_factory()
            async with session_factory() as session:
                await session.merge(SourceConfig(id=source_config_id, name="同刻书籍源"))
                session.add(
                    EntityType(
                        id=entity_type_id,
                        type=f"concept_{entity_type_id[:8]}",
                        name="概念",
                    )
                )
                session.add(
                    Entity(
                        id=entity_id,
                        source_config_id=source_config_id,
                        entity_type_id=entity_type_id,
                        type="concept",
                        name="书中人物",
                        normalized_name="书中人物",
                        description="全书事件共享的实体",
                    )
                )
                await session.flush()
                for rank, event_id in enumerate(chapter_ids):
                    session.add(
                        SourceEvent(
                            id=event_id,
                            source_config_id=source_config_id,
                            source_type="ARTICLE",
                            source_id="book-doc",
                            title=f"章节 {rank:02d}",
                            summary="整本书的事件共享同一导入时刻。",
                            content="测试内容",
                            category="书籍",
                            chunk_id=f"chunk-{rank // 2}",
                            rank=rank,
                            start_time=imported_at,
                            created_time=imported_at,
                        )
                    )
                    session.add(
                        EventEntity(
                            id=uuid.uuid4().hex,
                            event_id=event_id,
                            entity_id=entity_id,
                            weight=1.0,
                        )
                    )
                await session.commit()

            async def timeline(
                cursor: str | None = None,
                snapshot_id: str | None = None,
                direction: str = "older",
            ):
                response = await client.post(
                    "/api/v1/universe/timeline",
                    headers=headers,
                    json={
                        "epoch": 7,
                        "source_id": source_id,
                        "limit": 6,
                        "direction": direction,
                        "cursor": cursor,
                        "snapshot_id": snapshot_id,
                    },
                )
                assert response.status_code == 200, response.text
                return response.json()

            pages = [await timeline()]
            while pages[-1]["page"]["has_more"]:
                pages.append(
                    await timeline(
                        pages[-1]["page"]["next_cursor"],
                        pages[0]["snapshot_id"],
                    )
                )

            assert [page["page"]["returned_bundles"] for page in pages] == [6, 2]
            assert all(page["total_events"] == 8 for page in pages)
            paged_event_ids = [
                bundle["event"]["id"]
                for page in pages
                for bundle in page["bundles"]
            ]
            # Entry point is chapter 0; flying deeper reads the book forward.
            assert paged_event_ids == chapter_ids
            assert [
                bundle["ordinal"] for page in pages for bundle in page["bundles"]
            ] == list(range(8))

            # Paging back toward the newer end recovers the same narrative order.
            recovered = await timeline(
                pages[1]["page"]["newer_cursor"],
                pages[0]["snapshot_id"],
                "newer",
            )
            assert [
                bundle["event"]["id"] for bundle in recovered["bundles"]
            ] == chapter_ids[:6]
            assert [bundle["ordinal"] for bundle in recovered["bundles"]] == [
                0,
                1,
                2,
                3,
                4,
                5,
            ]
