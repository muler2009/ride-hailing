"""
Driver location lives in Redis only, per the architecture doc's rule
against persisting every GPS-adjacent update to PostgreSQL. Two keys per
driver:

  - a single GEO set (`locations:driver_positions`) holding every online
    driver's current position, queried via GEOSEARCH for nearby candidates
    (used by apps/dispatch for candidate search) and for live tracking
    (used by this app's WebSocket consumer)
  - a per-driver freshness key (`locations:driver_seen:{id}`) with a short
    TTL, so a driver who stops sending updates (app closed, phone died,
    network drop) silently ages out of dispatch candidacy without needing
    an explicit "went offline" signal — the architecture doc's stale-driver
    detection, implemented here as a check at query time rather than a
    background sweep.

This module owns the ephemeral, high-churn current-position store.
Sampled, persisted history lives in this app's DriverLocation model
(see models.py and services.py) — a deliberately separate, much
lower-write-volume table.
"""
from functools import lru_cache

import redis
from django.conf import settings

GEO_KEY = "locations:driver_positions"
SEEN_KEY_TEMPLATE = "locations:driver_seen:{driver_id}"
LOCATION_TTL_SECONDS = 90  # matches the 60-90s window suggested in the architecture doc


@lru_cache(maxsize=1)
def get_redis_client() -> "redis.Redis":
    return redis.Redis.from_url(settings.REDIS_URL, decode_responses=True)


def clear_redis_client_cache() -> None:
    """Used by tests when settings.REDIS_URL changes between test runs."""
    get_redis_client.cache_clear()


def set_driver_location(driver_id, latitude: float, longitude: float) -> None:
    client = get_redis_client()
    pipe = client.pipeline()
    pipe.geoadd(GEO_KEY, [longitude, latitude, str(driver_id)])
    pipe.set(SEEN_KEY_TEMPLATE.format(driver_id=driver_id), "1", ex=LOCATION_TTL_SECONDS)
    pipe.execute()


def remove_driver_location(driver_id) -> None:
    client = get_redis_client()
    pipe = client.pipeline()
    pipe.zrem(GEO_KEY, str(driver_id))
    pipe.delete(SEEN_KEY_TEMPLATE.format(driver_id=driver_id))
    pipe.execute()


def is_location_fresh(driver_id) -> bool:
    return get_redis_client().exists(SEEN_KEY_TEMPLATE.format(driver_id=driver_id)) == 1


def get_driver_location(driver_id):
    """Returns (latitude, longitude) or None if the driver has no current location."""
    result = get_redis_client().geopos(GEO_KEY, str(driver_id))
    if not result or result[0] is None:
        return None
    longitude, latitude = result[0]
    return (latitude, longitude)


def find_nearby_driver_ids(latitude: float, longitude: float, radius_km: float, count: int):
    """
    Returns a list of (driver_id, distance_km) tuples, nearest first, for
    online drivers within radius_km — WITHOUT filtering by freshness,
    approval, availability, or vehicle type. Those are business-logic
    filters applied by apps/dispatch/services.py; this function only knows
    about geography.
    """
    client = get_redis_client()
    results = client.geosearch(
        GEO_KEY,
        longitude=longitude,
        latitude=latitude,
        radius=radius_km,
        unit="km",
        sort="ASC",
        count=count,
        withdist=True,
    )
    return [(driver_id, float(distance)) for driver_id, distance in results]
