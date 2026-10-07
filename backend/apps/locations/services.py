"""
The ingestion path from the architecture doc's real-time section:
validate -> throttle -> write current position to Redis -> broadcast to
subscribed riders -> occasionally persist a sampled snapshot to Postgres.

record_location_update() is the single entry point every location update
goes through (the REST endpoint in views.py calls it, and nothing else
writes to Redis or DriverLocation directly), matching this codebase's
established pattern of one function owning a whole write path.
"""
from django.utils import timezone

from apps.locations import geo
from apps.locations.models import DriverLocation

MIN_UPDATE_INTERVAL_SECONDS = 3  # floor matching the "every 3-5s" cadence in the architecture doc
SNAPSHOT_INTERVAL_SECONDS = 30  # how often a raw update gets sampled into persisted history

_THROTTLE_KEY_TEMPLATE = "locations:throttle:{driver_id}"


class LocationUpdateThrottled(Exception):
    pass


def _is_throttled(driver_id) -> bool:
    client = geo.get_redis_client()
    # SET ... NX EX: only succeeds if the key doesn't already exist, atomically
    # setting it with a TTL — a lightweight per-driver rate limit with no
    # separate read-then-write race.
    acquired = client.set(
        _THROTTLE_KEY_TEMPLATE.format(driver_id=driver_id), "1", nx=True, ex=MIN_UPDATE_INTERVAL_SECONDS
    )
    return not acquired


def _get_active_ride_for_driver(driver):
    from apps.rides.models import TERMINAL_STATUSES, Ride

    return Ride.objects.filter(driver=driver).exclude(status__in=TERMINAL_STATUSES).first()


def _should_persist_snapshot(ride) -> bool:
    last = DriverLocation.objects.filter(ride=ride).order_by("-recorded_at").first()
    if last is None:
        return True
    return (timezone.now() - last.recorded_at).total_seconds() >= SNAPSHOT_INTERVAL_SECONDS


def broadcast_to_ride(ride_id, payload: dict) -> None:
    """
    Sends `payload` to every WebSocket connection subscribed to this ride
    (apps/locations/consumers.py:RideTrackingConsumer). Best-effort: if the
    channel layer is unavailable for any reason, a location update should
    never fail because nobody happened to be listening.
    """
    try:
        from asgiref.sync import async_to_sync
        from channels.layers import get_channel_layer

        channel_layer = get_channel_layer()
        if channel_layer is None:
            return
        async_to_sync(channel_layer.group_send)(f"ride_{ride_id}", payload)
    except Exception:
        import logging

        logging.getLogger(__name__).exception("Failed to broadcast to ride %s", ride_id)


def record_location_update(
    driver, *, latitude: float, longitude: float, heading=None, speed=None, accuracy=None
) -> None:
    """
    The full ingestion path for one location update from a driver's app:

      1. throttle (reject if this driver updated too recently)
      2. write current position to Redis (source of truth for "where is
         this driver right now")
      3. if the driver has an active ride, broadcast the position to
         anyone tracking that ride over WebSocket
      4. sample a persisted snapshot at a much lower rate than the raw
         update frequency

    Raises LocationUpdateThrottled if called too soon after the driver's
    last update — the view layer turns that into a 429.
    """
    if _is_throttled(driver.id):
        raise LocationUpdateThrottled(
            f"Location updates are limited to one every {MIN_UPDATE_INTERVAL_SECONDS} seconds."
        )

    geo.set_driver_location(driver.id, latitude=latitude, longitude=longitude)

    ride = _get_active_ride_for_driver(driver)
    if ride is None:
        return

    recorded_at = timezone.now()

    broadcast_to_ride(
        ride.id,
        {
            "type": "location.update",
            "latitude": str(latitude),
            "longitude": str(longitude),
            "heading": str(heading) if heading is not None else None,
            "speed": str(speed) if speed is not None else None,
            "recorded_at": recorded_at.isoformat(),
        },
    )

    if _should_persist_snapshot(ride):
        DriverLocation.objects.create(
            driver=driver,
            ride=ride,
            latitude=latitude,
            longitude=longitude,
            heading=heading,
            speed=speed,
            accuracy=accuracy,
            recorded_at=recorded_at,
        )
