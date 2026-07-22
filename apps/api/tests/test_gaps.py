"""回归：删除信源收尾（绑定清理 + 引擎槽释放 + 上传目录移除）、注册开关。"""

import os

import httpx
import pytest


@pytest.mark.asyncio
async def test_delete_cleanup_and_registration():
    from sag_api.core.config import settings
    from sag_api.core.db import SessionLocal
    from sag_api.db.models import Source
    from sag_api.main import app

    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            tok = (
                await c.post(
                    "/api/v1/auth/register", json={"email": "gap@x.com", "password": "password123"}
                )
            ).json()["access_token"]
            H = {"Authorization": f"Bearer {tok}"}

            src = (await c.post("/api/v1/sources", headers=H, json={"name": "手册"})).json()
            sid = src["id"]
            await c.post(
                f"/api/v1/sources/{sid}/documents",
                headers=H,
                files={"file": ("a.md", b"# t\nhello\n", "text/markdown")},
            )
            upload_dir = os.path.join(settings.upload_dir, sid)
            assert os.path.isdir(upload_dir), "上传目录应已创建"

            agent = (await c.post("/api/v1/agents", headers=H, json={"name": "小助"})).json()
            await c.post(
                f"/api/v1/agents/{agent['id']}/bindings",
                headers=H,
                json={"target_type": "source", "target_id": sid},
            )

            # 删除信源 = 绑定清理 + 引擎槽释放 + 上传目录移除
            async with SessionLocal() as s:
                sag_id = (await s.get(Source, sid)).sag_source_config_id
            await app.state.engine_manager.provision(sag_id)
            assert sag_id in app.state.engine_manager._slots

            assert (await c.delete(f"/api/v1/sources/{sid}", headers=H)).status_code == 200
            bindings = (await c.get(f"/api/v1/agents/{agent['id']}/bindings", headers=H)).json()
            assert bindings == [], "指向已删信源的绑定应被清理"
            assert sag_id not in app.state.engine_manager._slots, "引擎槽应已释放"
            assert not os.path.exists(upload_dir), "上传目录应已删除"

            # 注册开关（首个用户后可关闭）
            settings.allow_registration = False
            try:
                r = await c.post(
                    "/api/v1/auth/register",
                    json={"email": "second@x.com", "password": "password123"},
                )
                assert r.status_code == 403
            finally:
                settings.allow_registration = True
