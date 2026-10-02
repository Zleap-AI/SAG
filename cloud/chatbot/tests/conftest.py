"""Offline bootstrap, temp databases, generated encryption keys; no deployment env files."""
# Environment and extension loaders must precede SAG imports.
import os
import tempfile

import pytest
from cryptography.fernet import Fernet

ROOT = tempfile.mkdtemp(prefix="sag-chatbot-tests-")
for name in list(os.environ):
    if name.startswith("SAG_CHATBOT_") or name == "SAG_LOCK_CHATBOT_CONFIG":
        del os.environ[name]
os.environ.update({
    "SAG_DATABASE_URL": f"sqlite+aiosqlite:///{ROOT}/app.db",
    "SAG_DATA_DIR": f"{ROOT}/engine", "SAG_UPLOAD_DIR": f"{ROOT}/uploads",
    "SAG_DSH_CONNECTION_FILE": f"{ROOT}/connection.json", "SAG_ENGINE_WARMUP_COUNT": "0",
    "SAG_LLM_API_KEY": "", "SAG_EMBEDDING_API_KEY": "", "SAG_MINERU_API_KEY": "",
    "SAG_AUTH_MODE": "password",
    "LITELLM_LOCAL_MODEL_COST_MAP": "True",
})

from sag_chatbot.bootstrap import create_app

create_app()  # Installation must happen before any affected modules are imported.

from sag_api.core.config import settings
from sag_chatbot import runtime as rt
from sag_chatbot.config import Environment


@pytest.fixture(autouse=True)
def isolate(monkeypatch):
    stock = settings.model_copy(deep=True)
    stock.llm_api_key = "stock-key"
    stock.llm_model = "stock-model"
    stock.llm_base_url = "https://stock.invalid/v1"
    stock.embedding_api_key = "stock-embedding-key"
    stock.embedding_base_url = "https://stock-embedding.invalid/v1"
    stock.embedding_schema_dimensions = 3
    stock.embedding_request_dimensions = 3
    env = Environment({"SAG_CHATBOT_CONFIG_ENCRYPTION_KEY": Fernet.generate_key().decode()})
    manager = rt.Manager(env, stock)
    manager.loaded = True
    monkeypatch.setattr(rt, "manager", manager)
    yield manager


@pytest.fixture
async def database(tmp_path):
    from sag_api.db.models import Setting
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/settings.db")
    async with engine.begin() as connection:
        await connection.run_sync(lambda sync: Setting.__table__.create(sync))
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()
