"""Isolated connection state and encrypted settings rows; all transports are controlled."""

import pytest
from cryptography.fernet import Fernet

from sag_api.core.chatbot_config import Environment
from sag_api.core.config import settings
from sag_api.services import chatbot_service as rt


@pytest.fixture(autouse=True)
def isolate(monkeypatch):
    rt.install_query_adapters()
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
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from sag_api.db.models import Setting

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/settings.db")
    async with engine.begin() as connection:
        await connection.run_sync(lambda sync: Setting.__table__.create(sync))
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()
