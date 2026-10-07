"""
ASGI config for the Ride-Hailing Platform.

Routes HTTP requests to Django as normal, and WebSocket connections to
Channels for real-time ride tracking (apps/locations). WebSocket auth uses
a custom Keycloak-token-in-query-string middleware rather than Channels'
built-in session-based AuthMiddlewareStack, since this platform's API
authentication is entirely Bearer-token based (see apps/locations/ws_auth.py).
"""

import os

from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

# get_asgi_application() must be called before importing anything that
# touches Django models/apps (Channels routing imports consumers, which
# import models) — this ordering avoids AppRegistryNotReady errors.
django_asgi_app = get_asgi_application()

from channels.routing import ProtocolTypeRouter, URLRouter  # noqa: E402

from apps.locations.routing import websocket_urlpatterns  # noqa: E402
from apps.locations.ws_auth import KeycloakWebSocketAuthMiddlewareStack  # noqa: E402

application = ProtocolTypeRouter(
    {
        "http": django_asgi_app,
        "websocket": KeycloakWebSocketAuthMiddlewareStack(URLRouter(websocket_urlpatterns)),
    }
)
