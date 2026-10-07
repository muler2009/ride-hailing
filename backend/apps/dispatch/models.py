from django.db import models

from apps.common.models import BaseModel


class DispatchOfferStatus(models.TextChoices):
    PENDING = "PENDING", "Pending"
    ACCEPTED = "ACCEPTED", "Accepted"
    DECLINED = "DECLINED", "Declined"
    EXPIRED = "EXPIRED", "Expired"
    SUPERSEDED = "SUPERSEDED", "Superseded"  # another driver accepted first


class DispatchOffer(BaseModel):
    """
    One row per (ride, candidate driver) pairing offered during dispatch.
    Multiple PENDING offers can exist for the same ride at once — the
    platform offers to several nearby drivers simultaneously and the first
    to accept wins, per the architecture doc's dispatch design ("prevent
    multiple drivers from accepting the same ride"). See
    apps/dispatch/services.py:accept_offer for how that race is resolved.

    A driver is never offered the same ride twice (unique constraint) —
    once declined or expired, they're simply excluded from later candidate
    pools for that ride rather than being re-offered.
    """

    ride = models.ForeignKey("rides.Ride", on_delete=models.CASCADE, related_name="dispatch_offers")
    driver = models.ForeignKey("drivers.Driver", on_delete=models.CASCADE, related_name="dispatch_offers")
    status = models.CharField(
        max_length=20, choices=DispatchOfferStatus.choices, default=DispatchOfferStatus.PENDING, db_index=True
    )
    distance_km = models.DecimalField(max_digits=6, decimal_places=3)
    expires_at = models.DateTimeField()
    responded_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "dispatch_offer"
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["ride", "driver"], name="uniq_dispatch_offer_per_ride_driver")
        ]
        indexes = [models.Index(fields=["ride", "status"])]

    def __str__(self):
        return f"Offer<ride={self.ride_id} driver={self.driver_id}> [{self.status}]"
