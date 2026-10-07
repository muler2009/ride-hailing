"""
Our HTTP API authenticates via a Keycloak Bearer token on every request
(apps/identity/authentication.py) — there's no Django session involved, so
Channels' built-in session-based AuthMiddlewareStack doesn't apply here.
Browsers also can't attach a custom Authorization header to a WebSocket
handshake, so the access token travels as a query parameter instead
(`wss://.../ws/rides/<id>/track/?token=<access_token>`), the standard
workaround for this WebSocket limitation.

A guest tracking a ride by tracking_token never needs this at all — that
route requires no `scope["user"]`, since the token in the URL path IS the
credential (see consumers.py).
"""
from urllib.parse import parse_qs

from channels.db import database_sync_to_async
from django.contrib.auth.models import AnonymousUser

from apps.identity.keycloak_client import KeycloakError, verify_access_token
from apps.identity.services import sync_user_from_keycloak_claims


@database_sync_to_async
def _resolve_user(token: str):
    try:
        claims = verify_access_token(token)
    except KeycloakError:
        return AnonymousUser()

    try:
        user = sync_user_from_keycloak_claims(claims)
    except ValueError:
        return AnonymousUser()

    if not user.is_active or user.is_suspended:
        return AnonymousUser()
    return user


class KeycloakWebSocketAuthMiddleware:
    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        query_string = scope.get("query_string", b"").decode("utf-8")
        token = parse_qs(query_string).get("token", [None])[0]

        scope["user"] = await _resolve_user(token) if token else AnonymousUser()
        return await self.inner(scope, receive, send)


def KeycloakWebSocketAuthMiddlewareStack(inner):
    return KeycloakWebSocketAuthMiddleware(inner)
