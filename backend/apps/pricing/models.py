from django.db import models

from apps.common.models import BaseModel


class PricingRule(BaseModel):
    """
    One rule per vehicle type — the rates apps/pricing/services.py's
    calculate_fare() reads to build a breakdown. Not scoped per
    ServiceArea (that entity doesn't exist yet in this codebase); adding
    it later would mean relaxing `vehicle_type` from OneToOne to a
    (vehicle_type, service_area) unique pair, not a structural rewrite.

    All money fields are Decimal, never float, per the architecture doc's
    explicit rule against floating-point arithmetic for money.
    `surge_multiplier` is a manually-set factor an operator adjusts (e.g.
    during a known high-demand window) — there is no automatic
    demand-based surge calculation in this phase.
    """

    vehicle_type = models.OneToOneField(
        "vehicles.VehicleType", on_delete=models.CASCADE, related_name="pricing_rule"
    )
    currency = models.CharField(max_length=3, default="USD")

    base_fare = models.DecimalField(max_digits=8, decimal_places=2)
    per_km_rate = models.DecimalField(max_digits=8, decimal_places=2)
    per_minute_rate = models.DecimalField(max_digits=8, decimal_places=2)
    per_minute_waiting_rate = models.DecimalField(max_digits=8, decimal_places=2, default=0)
    minimum_fare = models.DecimalField(max_digits=8, decimal_places=2)
    cancellation_fee = models.DecimalField(max_digits=8, decimal_places=2, default=0)
    surge_multiplier = models.DecimalField(max_digits=4, decimal_places=2, default=1)
    tax_rate = models.DecimalField(
        max_digits=5, decimal_places=4, default=0, help_text="Fraction, e.g. 0.15 for 15%."
    )

    class Meta:
        db_table = "pricing_rule"
        ordering = ["vehicle_type__name"]

    def __str__(self):
        return f"PricingRule<{self.vehicle_type.name}>"


class FareType(models.TextChoices):
    ESTIMATE = "ESTIMATE", "Estimate"
    FINAL = "FINAL", "Final"
    CANCELLATION = "CANCELLATION", "Cancellation Fee"


class Fare(BaseModel):
    """
    One breakdown for a ride. A ride can have more than one Fare row over
    its lifetime — an ESTIMATE at request time, a FINAL at completion, and
    a CANCELLATION if the rider cancelled late enough to owe a fee — but
    at most one of each type, per the unique constraint below.

    FINAL and CANCELLATION are immutable once created (apps/pricing/services.py
    raises rather than overwriting); ESTIMATE may be recalculated, since
    it's disposable by nature.
    """

    ride = models.ForeignKey("rides.Ride", on_delete=models.CASCADE, related_name="fares")
    fare_type = models.CharField(max_length=20, choices=FareType.choices)
    currency = models.CharField(max_length=3, default="USD")
    distance_km = models.DecimalField(max_digits=7, decimal_places=2)
    duration_minutes = models.DecimalField(max_digits=6, decimal_places=1)
    total_amount = models.DecimalField(max_digits=10, decimal_places=2)

    class Meta:
        db_table = "pricing_fare"
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["ride", "fare_type"], name="uniq_fare_per_ride_and_type")
        ]

    def __str__(self):
        return f"Fare<{self.ride_id}:{self.fare_type}> {self.currency} {self.total_amount}"


class FareLineItemType(models.TextChoices):
    BASE_FARE = "BASE_FARE", "Base Fare"
    DISTANCE_FARE = "DISTANCE_FARE", "Distance"
    DURATION_FARE = "DURATION_FARE", "Duration"
    WAITING_FARE = "WAITING_FARE", "Waiting Time"
    SURGE_ADJUSTMENT = "SURGE_ADJUSTMENT", "Surge"
    TOLLS = "TOLLS", "Tolls"
    MINIMUM_FARE_ADJUSTMENT = "MINIMUM_FARE_ADJUSTMENT", "Minimum Fare Adjustment"
    TAX = "TAX", "Tax"
    DISCOUNT = "DISCOUNT", "Discount"
    CANCELLATION_FEE = "CANCELLATION_FEE", "Cancellation Fee"


class FareLineItem(BaseModel):
    """
    One row per component of a Fare's breakdown, in display order. Signed:
    everything is positive except DISCOUNT, which is negative — so
    `sum(line_items.amount) == fare.total_amount` always holds exactly,
    with no separate rounding step required to reconcile them.
    """

    fare = models.ForeignKey(Fare, on_delete=models.CASCADE, related_name="line_items")
    item_type = models.CharField(max_length=30, choices=FareLineItemType.choices)
    label = models.CharField(max_length=100)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    sequence = models.PositiveSmallIntegerField()

    class Meta:
        db_table = "pricing_fare_line_item"
        ordering = ["fare", "sequence"]

    def __str__(self):
        return f"{self.label}: {self.amount}"
