from django.conf import settings
from django.db import models

from apps.common.models import BaseModel


class DriverLocation(BaseModel):
    """
    A sampled snapshot of a driver's position, persisted for trip playback
    and dispute resolution — NOT a record of every raw GPS ping (those
    live in Redis only; see apps/locations/geo.py). Written at a bounded
    rate by apps/locations/services.py:record_location_update, roughly
    every SNAPSHOT_INTERVAL_SECONDS while the driver has an active ride.

    `ride` is nullable because a driver can send location updates while
    online but not currently on a trip (useful for the live map an
    operator might watch); those updates still refresh Redis but are not
    sampled into history, since there's nothing to play back yet — in
    practice this field will almost always be set when a row exists at
    all, but it's kept nullable for that edge case rather than forcing a
    row to pretend it belongs to a ride it doesn't.
    """

    driver = models.ForeignKey("drivers.Driver", on_delete=models.CASCADE, related_name="location_history")
    ride = models.ForeignKey(
        "rides.Ride", on_delete=models.SET_NULL, null=True, blank=True, related_name="location_history"
    )
    latitude = models.DecimalField(max_digits=9, decimal_places=6)
    longitude = models.DecimalField(max_digits=9, decimal_places=6)
    heading = models.DecimalField(
        max_digits=5, decimal_places=2, null=True, blank=True, help_text="Compass heading, 0-360 degrees."
    )
    speed = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True, help_text="km/h.")
    accuracy = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True, help_text="meters.")
    recorded_at = models.DateTimeField(help_text="Client-reported timestamp of this position.")

    class Meta:
        db_table = "locations_driver_location"
        ordering = ["recorded_at"]
        indexes = [
            models.Index(fields=["ride", "recorded_at"]),
            models.Index(fields=["driver", "recorded_at"]),
        ]

    def __str__(self):
        return f"DriverLocation<{self.driver_id}@{self.recorded_at}>"
