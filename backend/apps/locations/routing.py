from django.urls import path

from apps.locations.consumers import RideTrackingConsumer

websocket_urlpatterns = [
    path("ws/rides/<uuid:ride_id>/track/", RideTrackingConsumer.as_asgi()),
    path("ws/rides/track/<uuid:tracking_token>/", RideTrackingConsumer.as_asgi()),
]
