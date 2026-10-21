"""Final tenant authority for client business requests."""

import os
from pathlib import Path
from time import time

from sag_api.client_access.core import ClientAccessError
from sag_api.fnos.client_access import FnOSClientAccess
from sag_api.fnos.identity import GatewayIdentity, InternalIdentitySigner, derive_fnos_internal_key
from sag_api.fnos.proxy import bearer_token


def install_worker_client_access(app, settings):
    from sag_api.client_access.middleware import ClientBusinessMiddleware

    identity = GatewayIdentity(settings.fnos_uid, settings.fnos_username, False)

    signer = InternalIdentitySigner.from_file(Path(settings.fnos_internal_secret_file))

    async def authority(request):
        from sag_api.core.errors import AuthError
        try:
            verified = signer.verify(request.headers, settings.fnos_uid, int(time()),
                                     expected_username=settings.fnos_username, expected_principal="client")
        except AuthError:
            raise ClientAccessError("invalid_grant", 401) from None
        request.state.client_identity = verified
        access = FnOSClientAccess(
            Path(os.environ["SAG_FNOS_DATA_ROOT"]),
            derive_fnos_internal_key(Path(settings.fnos_internal_secret_file), b"sag-client-access-v1"),
        )
        if not access.policy()["enabled"]:
            raise ClientAccessError("client_access_disabled", 403)
        token = bearer_token(request.scope["headers"])
        if not token:
            raise ClientAccessError("invalid_grant", 401)
        store = access.store(identity)
        claims = store.authenticate(token)
        route = claims["route"]
        if (
            claims["subject"] != f"fnos_{identity.uid}"
            or route.get("uid") != identity.uid
            or route.get("username") != identity.username
            or await access.account(identity) != route.get("account_instance")
        ):
            raise ClientAccessError("invalid_grant", 401)
        return store, claims

    app.add_middleware(
        ClientBusinessMiddleware, authority=authority, max_body_bytes=settings.max_upload_mb * 1024 * 1024 + 1024 * 1024
    )
