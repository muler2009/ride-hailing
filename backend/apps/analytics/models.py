"""
Reporting & Analytics bounded context (architecture doc, Section B): "no
new data — read-only projections/materialized views." These two tables
are exactly that: a periodically-computed summary of what other apps'
tables already record, never a source of truth for anything.

This is the deliberate counterpart to apps/admin_api's dashboard
(Phase 12), which computes the same *kinds* of numbers live, on demand,
over a rolling window, and says so in its own docstring:

    "At serious volume the ride/revenue rollups would move to
    materialized views or a periodically-refreshed summary table
    (Phase 13's territory), but computing them live keeps them exactly
    correct with no staleness window, which matters more right now."

Phase 13 is that "serious volume" answer for the question Phase 12
structurally can't answer well: trends *over time*. A live query
answers "what's happening right now"; scanning raw Ride/Payment/
DriverEarning tables for a 6-month trend chart means re-scanning the
same closed, unchanging days on every request. A daily rollup, computed
once per closed day and never touched again, turns that into a read of
a handful of pre-summed rows.

Deliberately NOT rolled up here: `active_rides` and `online_drivers`
(Phase 12's dashboard). Both are point-in-time, ephemeral facts — there
is no historical "active rides as of last Tuesday" to reconstruct once
Tuesday is over, so a daily rollup has nothing meaningful to store for
them. Everything below is instead built from durable, append-style
records (Ride, DispatchOffer, Payment, DriverEarning) that still exist
and still mean the same thing regardless of when you look.
"""
from django.db import models

from apps.common.models import BaseModel


class DailyPlatformRollup(BaseModel):
    """
    One row per UTC calendar day. Written exactly once by
    `apps.analytics.services.compute_daily_rollup`, then only ever
    overwritten by a deliberate recompute (via `update_or_create` on
    `date`) — never incrementally patched, same reasoning the
    architecture doc gives for derived reputation scores (Phase 11) and
    wallet balances (Phase 9): an exact recompute from source data is
    cheap and free of drift; a hand-maintained running total is not.

    Two different "which day" conventions are used side by side here,
    matching what Phase 12's live queries already do, not a new choice
    invented for this table:

    - Ride-shaped counts (`rides_*`, `offers_*`, `active_drivers`,
      `payment_failures`) bucket by the day the *ride/offer/payment
      record itself* was created — same as
      `admin_api.services.get_dashboard_summary` and
      `get_dispatch_health`, which filter on `created_at` of the record
      being counted.
    - Revenue fields (`gross_revenue`, `platform_commission`,
      `driver_earnings`, `rides_with_earning`, `average_fare`) bucket by
      the day the `DriverEarning` row was created — same as
      `admin_api.services.get_revenue_report`. Because earnings are
      credited asynchronously (Phase 9, via Celery off the `PAID`
      event) shortly after a ride completes, this is very rarely a
      different calendar day than the ride's own `created_at`, but it
      can be for a ride that completes right at midnight — an accepted,
      documented approximation rather than a silent one.

    Unlike Phase 12's `get_revenue_report`, the per-vehicle-type
    breakdown here (`DailyVehicleTypeRollup`) uses this SAME earnings-day
    window as the totals above (rather than the ride's own `created_at`,
    which is what the live revenue report uses for its breakdown). That
    is an intentional improvement made possible by not needing the
    breakdown query to also double as a live dashboard call: it keeps a
    single day's total revenue and that same day's per-vehicle-type
    revenue always summing to the same number, which the live version
    does not strictly guarantee.
    """

    date = models.DateField(unique=True, db_index=True)

    # Ride-outcome counts — Ride.created_at bucketed by day.
    rides_requested = models.PositiveIntegerField(default=0)
    rides_completed = models.PositiveIntegerField(default=0)
    rides_cancelled = models.PositiveIntegerField(default=0)
    rides_no_driver_found = models.PositiveIntegerField(default=0)

    # Distinct drivers who completed at least one ride requested this day.
    active_drivers = models.PositiveIntegerField(default=0)

    # Dispatch health — DispatchOffer.created_at bucketed by day.
    offers_sent = models.PositiveIntegerField(default=0)
    offers_accepted = models.PositiveIntegerField(default=0)
    offers_expired = models.PositiveIntegerField(default=0)
    offers_declined = models.PositiveIntegerField(default=0)

    # Payments — Payment.created_at bucketed by day.
    payment_failures = models.PositiveIntegerField(default=0)

    # Revenue — DriverEarning.created_at bucketed by day (see class docstring).
    rides_with_earning = models.PositiveIntegerField(default=0)
    gross_revenue = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    platform_commission = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    driver_earnings = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    average_fare = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)

    class Meta:
        db_table = "analytics_daily_platform_rollup"
        ordering = ["-date"]

    def __str__(self):
        return f"DailyPlatformRollup<{self.date}>"

    @property
    def offer_acceptance_rate(self):
        """Null (not 0) when there was no dispatch activity that day — same convention as Phase 12."""
        return round(self.offers_accepted / self.offers_sent, 3) if self.offers_sent else None

    @property
    def no_driver_found_rate(self):
        return round(self.rides_no_driver_found / self.rides_requested, 3) if self.rides_requested else None


class DailyVehicleTypeRollup(BaseModel):
    """
    Per-vehicle-type revenue slice of a `DailyPlatformRollup` day. A
    separate table rather than a JSON blob column on the parent row so
    each vehicle type's numbers stay queryable/indexable on their own
    (e.g. "Moto revenue trend over the last quarter") — the same
    normalize-don't-blob instinct behind every other breakdown table in
    this codebase (e.g. `RideStatusHistory`, `FareLineItem`).

    Rows for a given date are fully replaced (delete-then-recreate)
    every time that date is (re)computed, rather than updated field by
    field — a vehicle type that had zero rides that day simply has no
    row, and a recompute can't leave a stale row behind for a vehicle
    type that existed on the first run but was retired before a rerun.
    """

    date = models.DateField(db_index=True)
    vehicle_type = models.ForeignKey(
        "vehicles.VehicleType", on_delete=models.PROTECT, related_name="daily_revenue_rollups"
    )
    ride_count = models.PositiveIntegerField(default=0)
    gross_revenue = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    platform_commission = models.DecimalField(max_digits=14, decimal_places=2, default=0)

    rollup = models.ForeignKey(
        DailyPlatformRollup, on_delete=models.CASCADE, related_name="vehicle_type_breakdowns"
    )

    class Meta:
        db_table = "analytics_daily_vehicle_type_rollup"
        ordering = ["-date", "-gross_revenue"]
        constraints = [
            models.UniqueConstraint(fields=["date", "vehicle_type"], name="uniq_vehicle_type_rollup_per_day")
        ]

    def __str__(self):
        return f"DailyVehicleTypeRollup<{self.date} {self.vehicle_type_id}>"
