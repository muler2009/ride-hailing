from django.conf import settings
from django.db import models

from apps.common.models import BaseModel


class RideStatus(models.TextChoices):
    REQUESTED = "REQUESTED", "Requested"
    SEARCHING_DRIVER = "SEARCHING_DRIVER", "Searching for Driver"
    DRIVER_ASSIGNED = "DRIVER_ASSIGNED", "Driver Assigned"
    DRIVER_ACCEPTED = "DRIVER_ACCEPTED", "Driver Accepted"
    DRIVER_ARRIVING = "DRIVER_ARRIVING", "Driver Arriving"
    DRIVER_ARRIVED = "DRIVER_ARRIVED", "Driver Arrived"
    TRIP_STARTED = "TRIP_STARTED", "Trip Started"
    TRIP_COMPLETED = "TRIP_COMPLETED", "Trip Completed"
    PAYMENT_PENDING = "PAYMENT_PENDING", "Payment Pending"
    PAID = "PAID", "Paid"
    RATED = "RATED", "Rated"
    CANCELLED_BY_RIDER = "CANCELLED_BY_RIDER", "Cancelled by Rider"
    CANCELLED_BY_DRIVER = "CANCELLED_BY_DRIVER", "Cancelled by Driver"
    EXPIRED = "EXPIRED", "Expired"
    NO_DRIVER_FOUND = "NO_DRIVER_FOUND", "No Driver Found"


TERMINAL_STATUSES = {
    RideStatus.RATED,
    RideStatus.CANCELLED_BY_RIDER,
    RideStatus.CANCELLED_BY_DRIVER,
    RideStatus.EXPIRED,
    RideStatus.NO_DRIVER_FOUND,
}

# Statuses from which the rider/driver may still cancel, per the
# architecture doc's ride state machine (Section D).
RIDER_CANCELLABLE_STATUSES = {
    RideStatus.REQUESTED,
    RideStatus.SEARCHING_DRIVER,
    RideStatus.DRIVER_ASSIGNED,
    RideStatus.DRIVER_ACCEPTED,
    RideStatus.DRIVER_ARRIVING,
    RideStatus.DRIVER_ARRIVED,
}
DRIVER_CANCELLABLE_STATUSES = {
    RideStatus.DRIVER_ASSIGNED,
    RideStatus.DRIVER_ACCEPTED,
    RideStatus.DRIVER_ARRIVING,
    RideStatus.DRIVER_ARRIVED,
}

# A rider cancelling from one of these statuses owes the vehicle type's
# flat cancellation_fee (apps/pricing) — the driver has already committed
# to (accepted) the ride, unlike DRIVER_ASSIGNED where an offer is merely
# pending and hasn't been accepted yet.
RIDER_CANCELLATION_FEE_STATUSES = {
    RideStatus.DRIVER_ACCEPTED,
    RideStatus.DRIVER_ARRIVING,
    RideStatus.DRIVER_ARRIVED,
}


def _generate_tracking_token():
    import uuid

    return uuid.uuid4()


class Ride(BaseModel):
    """
    A ride request. The requester is EITHER a registered User (`rider`) OR
    an unregistered guest identified only by a phone number
    (`guest_phone_number`) — never both, never neither, enforced by the
    CheckConstraint below. This is a deliberate product decision: signup
    is not required to request a ride.

    A guest has no login session, so `tracking_token` is their credential
    for checking status or cancelling afterwards (see
    apps/rides/views.py's GuestRideTrackView/GuestRideCancelView) — the
    same pattern as a package-tracking or guest-checkout number. It costs
    nothing extra for a registered rider's ride to also carry one; it's
    simply unused in that path.
    """

    rider = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="rides_as_rider",
    )
    guest_phone_number = models.CharField(max_length=32, null=True, blank=True, db_index=True)
    guest_name = models.CharField(max_length=150, blank=True)

    tracking_token = models.UUIDField(
        default=_generate_tracking_token, unique=True, editable=False, db_index=True
    )

    driver = models.ForeignKey(
        "drivers.Driver",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="rides_as_driver",
    )
    vehicle_type = models.ForeignKey(
        "vehicles.VehicleType", on_delete=models.PROTECT, related_name="rides"
    )

    status = models.CharField(
        max_length=20, choices=RideStatus.choices, default=RideStatus.REQUESTED, db_index=True
    )
    # Incremented on every transition. Defense-in-depth alongside
    # select_for_update in services.py — see the architecture doc's note on
    # combining optimistic and pessimistic locking for ride state changes.
    version = models.PositiveIntegerField(default=0)

    pickup_address = models.CharField(max_length=255)
    pickup_latitude = models.DecimalField(max_digits=9, decimal_places=6)
    pickup_longitude = models.DecimalField(max_digits=9, decimal_places=6)
    destination_address = models.CharField(max_length=255)
    destination_latitude = models.DecimalField(max_digits=9, decimal_places=6)
    destination_longitude = models.DecimalField(max_digits=9, decimal_places=6)

    # Best-effort, computed at creation time via apps.maps (Phase 6) — null
    # if the map provider was unreachable or the route couldn't be found;
    # ride creation never fails because of this. Phase 7's pricing engine
    # will need a real value here (or a fallback/retry path for when it's
    # null), but that's Phase 7's problem to solve, not this field's.
    estimated_distance_km = models.DecimalField(max_digits=7, decimal_places=2, null=True, blank=True)
    estimated_duration_minutes = models.DecimalField(max_digits=6, decimal_places=1, null=True, blank=True)

    driver_assigned_at = models.DateTimeField(null=True, blank=True)
    driver_accepted_at = models.DateTimeField(null=True, blank=True)
    trip_started_at = models.DateTimeField(null=True, blank=True)
    trip_completed_at = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancellation_reason = models.CharField(max_length=255, blank=True)

    class Meta:
        db_table = "rides_ride"
        ordering = ["-created_at"]
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(rider__isnull=False, guest_phone_number__isnull=True)
                    | models.Q(rider__isnull=True, guest_phone_number__isnull=False)
                ),
                name="ride_exactly_one_of_rider_or_guest_phone",
            )
        ]
        indexes = [models.Index(fields=["status", "created_at"])]

    def __str__(self):
        who = self.rider.email if self.rider_id else self.guest_phone_number
        return f"Ride<{who}> [{self.status}]"

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    @property
    def is_guest_ride(self) -> bool:
        return self.rider_id is None


class RideStatusHistory(BaseModel):
    """
    Append-only audit trail of every transition a ride goes through — never
    updated or deleted, per the architecture doc's data-integrity rules for
    ride/financial history tables.
    """

    ride = models.ForeignKey(Ride, on_delete=models.CASCADE, related_name="status_history")
    from_status = models.CharField(max_length=20, blank=True)
    to_status = models.CharField(max_length=20)
    changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="ride_status_changes",
        help_text="Null for system-triggered transitions or guest-initiated actions.",
    )
    note = models.CharField(max_length=255, blank=True)

    class Meta:
        db_table = "rides_ride_status_history"
        ordering = ["created_at"]

    def __str__(self):
        return f"{self.ride_id}: {self.from_status or '—'} -> {self.to_status}"
