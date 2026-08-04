import pytest
from fastapi import FastAPI

from sag_api.main import lifespan, settings


@pytest.mark.asyncio
async def test_production_lifespan_rejects_secret_shorter_than_32_bytes(monkeypatch):
    monkeypatch.setattr(settings, "environment", "prod")
    monkeypatch.setattr(settings, "secret_key", "too-short")

    with pytest.raises(RuntimeError, match="32"):
        async with lifespan(FastAPI()):
            pass


@pytest.mark.asyncio
async def test_production_password_mode_rejects_missing_bootstrap_secret(monkeypatch):
    """Catches fnOS starting with a LAN-reachable password reset gate that has no secret."""
    monkeypatch.setattr(settings, "environment", "prod")
    monkeypatch.setattr(settings, "secret_key", "s" * 32)
    monkeypatch.setattr(settings, "auth_mode", "password")
    monkeypatch.setitem(settings.__dict__, "auth_bootstrap_token", settings.auth_bootstrap_token.__class__(""))

    with pytest.raises(RuntimeError, match="SAG_AUTH_BOOTSTRAP_TOKEN"):
        async with lifespan(FastAPI()):
            pass


@pytest.mark.asyncio
async def test_production_password_mode_requires_bootstrap_separate_from_session_secret(
    monkeypatch,
):
    """Catches a single leaked value granting both session forgery and password reset."""
    duplicate = "d" * 32
    monkeypatch.setattr(settings, "environment", "prod")
    monkeypatch.setattr(settings, "secret_key", duplicate)
    monkeypatch.setattr(settings, "auth_mode", "password")
    monkeypatch.setitem(
        settings.__dict__,
        "auth_bootstrap_token",
        settings.auth_bootstrap_token.__class__(duplicate),
    )

    with pytest.raises(RuntimeError, match="彼此独立"):
        async with lifespan(FastAPI()):
            pass
