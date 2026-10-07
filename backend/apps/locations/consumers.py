from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer


class RideTrackingConsumer(AsyncJsonWebsocketConsumer):
    """
    One WebSocket connection per ride being tracked. Two routes share this
    consumer (see routing.py):

      - ws/rides/<uuid:ride_id>/track/           — registered rider/driver/admin
      - ws/rides/track/<uuid:tracking_token>/    — guest, no login

    Authorization mirrors the REST endpoints exactly: a registered caller
    must be the ride's own rider, its assigned driver, or hold
    `ride.manage`; a guest is authorized purely by knowing the correct
    tracking_token, same as GuestRideTrackView. Broadcasts are sent by
    apps/locations/services.py (location updates) and
    apps/rides/services.py (status changes) via the channel layer group
    `ride_{id}` — this consumer only relays them to the connected client.
    """

    async def connect(self):
        ride = await self._resolve_and_authorize_ride()
        if ride is None:
            await self.close(code=4404)
            return

        self.ride_id = str(ride.id)
        self.group_name = f"ride_{self.ride_id}"
        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()

    async def disconnect(self, close_code):
        group_name = getattr(self, "group_name", None)
        if group_name:
            await self.channel_layer.group_discard(group_name, self.channel_name)

    async def location_update(self, event):
        await self.send_json({k: v for k, v in event.items() if k != "type"} | {"event": "location_update"})

    async def status_update(self, event):
        await self.send_json({k: v for k, v in event.items() if k != "type"} | {"event": "status_update"})

    @database_sync_to_async
    def _resolve_and_authorize_ride(self):
        from apps.identity.services import user_has_permission
        from apps.rides.models import Ride

        tracking_token = self.scope["url_route"]["kwargs"].get("tracking_token")
        ride_id = self.scope["url_route"]["kwargs"].get("ride_id")

        if tracking_token is not None:
            return Ride.objects.filter(tracking_token=tracking_token).first()

        user = self.scope.get("user")
        if user is None or not user.is_authenticated:
            return None

        try:
            ride = Ride.objects.get(id=ride_id)
        except Ride.DoesNotExist:
            return None

        driver = getattr(user, "driver_profile", None)
        is_own_rider = ride.rider_id == user.id
        is_assigned_driver = driver is not None and ride.driver_id == driver.id
        is_admin_viewer = user_has_permission(user, "ride.manage")

        if is_own_rider or is_assigned_driver or is_admin_viewer:
            return ride
        return None
