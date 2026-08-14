from __future__ import annotations

import pytest
from starlette.requests import Request

from sag_api.core.config import settings
from sag_api.core.deps import get_fnos_identity, require_fnos_nas_admin
from sag_api.core.errors import AuthError, ForbiddenError, NotFoundError
from sag_api.fnos.identity import GatewayIdentity


def _request() -> Request:
    return Request({"type": "http", "method": "GET", "path": "/", "headers": []})


def test_get_fnos_identity_returns_only_verified_request_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(settings.__dict__, "auth_mode", "fnos")
    request = _request()
    expected = GatewayIdentity(uid=1000, username="Alice", is_admin=True)
    request.state.fnos_identity = expected

    assert get_fnos_identity(request) is expected


def test_get_fnos_identity_hides_feature_outside_fnos_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(settings.__dict__, "auth_mode", "password")
    request = _request()
    request.state.fnos_identity = GatewayIdentity(uid=1000, username="Alice", is_admin=True)

    with pytest.raises(NotFoundError, match="资源不存在"):
        get_fnos_identity(request)


def test_get_fnos_identity_rejects_missing_or_untyped_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(settings.__dict__, "auth_mode", "fnos")
    with pytest.raises(AuthError, match="fnOS 身份未验证"):
        get_fnos_identity(_request())

    request = _request()
    request.state.fnos_identity = {"uid": 1000, "is_admin": True}
    with pytest.raises(AuthError, match="fnOS 身份未验证"):
        get_fnos_identity(request)


def test_require_fnos_nas_admin_returns_admin_identity() -> None:
    identity = GatewayIdentity(uid=1000, username="Alice", is_admin=True)
    assert require_fnos_nas_admin(identity) is identity


def test_require_fnos_nas_admin_rejects_non_admin_without_folder_data() -> None:
    identity = GatewayIdentity(uid=1000, username="Alice", is_admin=False)

    with pytest.raises(ForbiddenError) as captured:
        require_fnos_nas_admin(identity)

    assert captured.value.code == "nas_administrator_required"
    assert captured.value.status_code == 403
    assert "folder" not in str(captured.value).lower()
    assert "/vol" not in str(captured.value)
