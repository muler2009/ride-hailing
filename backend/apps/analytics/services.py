"""
Rollup computation (write path) and trend/summary read helpers for the
Reporting & Analytics context.

Deliberately independent of `apps.admin_api`: Admin ("composes other
contexts") and Reporting & Analytics ("read-only projections/materialized
views") are two distinct bounded contexts per the architecture doc, and
each should read directly from the source-of-truth apps (rides, dispatch,
payments, earnings) rather than one composing layer depending on the
other. The two do compute the same *kinds* of numbers because they answer
the same operational questions on different time axes — that's expected
convergent structure, not a reason to share code between them.
"""
from datetime import datetime, time, timedelta

from django.db import transaction
from django.db.models import Count, Sum
from django.utils import timezone

from apps.analytics.models import DailyPlatformRollup, DailyVehicleTypeRollup
from apps.common.money import ZERO, quantize_money
from apps.dispatch.models import DispatchOffer, DispatchOfferStatus
from apps.earnings.models import DriverEarning
from apps.payments.models import Payment, PaymentStatus
from apps.rides.models import Ride, RideStatus

COMPLETED_STATUSES = [RideStatus.TRIP_COMPLETED, RideStatus.PAYMENT_PENDING, RideStatus.PAID, RideStatus.RATED]
CANCELLED_STATUSES = [RideStatus.CANCELLED_BY_RIDER, RideStatus.CANCELLED_BY_DRIVER]


def _day_window(target_date):
    """The [start, end) UTC instants spanning target_date's calendar day."""
    start = timezone.make_aware(datetime.combine(target_date, time.min))
    return start, start + timedelta(days=1)


def compute_daily_rollup(target_date) -> DailyPlatformRollup:
    """
    (Re)computes and upserts the rollup for exactly one UTC calendar day,
    from scratch, from raw data — never by patching a prior value. Safe
    to call repeatedly for the same date (a backfill rerun, a retried
    Celery task): `update_or_create` on the unique `date` plus a
    delete-then-recreate of that date's vehicle-type rows means the
    result is always exactly what a fresh computation would produce,
    regardless of how many times it's run.

    Refuses to compute today or a future date: a day isn't "historical"
    until it's over, and a rollup that gets silently smaller than a
    number someone already saw (because it was computed mid-day and
    later re-run) is a worse failure mode than simply not having
    today's row yet — that's what Phase 12's live dashboard is for.
    """
    today = timezone.localdate()
    if target_date >= today:
        raise ValueError(f"{target_date} is not a closed day yet (today is {today}); only past days can be rolled up.")

    window_start, window_end = _day_window(target_date)

    rides = Ride.objects.filter(created_at__gte=window_start, created_at__lt=window_end)
    completed_rides = rides.filter(status__in=COMPLETED_STATUSES)
    active_drivers = (
        completed_rides.filter(driver__isnull=False).values("driver_id").distinct().count()
    )

    offers = DispatchOffer.objects.filter(created_at__gte=window_start, created_at__lt=window_end)

    payment_failures = Payment.objects.filter(
        created_at__gte=window_start, created_at__lt=window_end, status=PaymentStatus.FAILED
    ).count()

    earnings = DriverEarning.objects.filter(created_at__gte=window_start, created_at__lt=window_end)
    earning_totals = earnings.aggregate(
        gross=Sum("fare_amount"), commission=Sum("commission_amount"), driver_share=Sum("driver_earning_amount"),
        count=Count("id"),
    )
    rides_with_earning = earning_totals["count"] or 0
    gross_revenue = earning_totals["gross"] or ZERO
    average_fare = quantize_money(gross_revenue / rides_with_earning) if rides_with_earning else None

    defaults = dict(
        rides_requested=rides.count(),
        rides_completed=completed_rides.count(),
        rides_cancelled=rides.filter(status__in=CANCELLED_STATUSES).count(),
        rides_no_driver_found=rides.filter(status=RideStatus.NO_DRIVER_FOUND).count(),
        active_drivers=active_drivers,
        offers_sent=offers.count(),
        offers_accepted=offers.filter(status=DispatchOfferStatus.ACCEPTED).count(),
        offers_expired=offers.filter(status=DispatchOfferStatus.EXPIRED).count(),
        offers_declined=offers.filter(status=DispatchOfferStatus.DECLINED).count(),
        payment_failures=payment_failures,
        rides_with_earning=rides_with_earning,
        gross_revenue=gross_revenue,
        platform_commission=earning_totals["commission"] or ZERO,
        driver_earnings=earning_totals["driver_share"] or ZERO,
        average_fare=average_fare,
    )

    with transaction.atomic():
        rollup, _created = DailyPlatformRollup.objects.update_or_create(date=target_date, defaults=defaults)

        by_vehicle_type = (
            earnings.values("ride__vehicle_type_id")
            .annotate(ride_count=Count("id"), gross=Sum("fare_amount"), commission=Sum("commission_amount"))
        )
        DailyVehicleTypeRollup.objects.filter(date=target_date).delete()
        DailyVehicleTypeRollup.objects.bulk_create(
            [
                DailyVehicleTypeRollup(
                    date=target_date,
                    rollup=rollup,
                    vehicle_type_id=row["ride__vehicle_type_id"],
                    ride_count=row["ride_count"],
                    gross_revenue=row["gross"] or ZERO,
                    platform_commission=row["commission"] or ZERO,
                )
                for row in by_vehicle_type
            ]
        )

    return rollup


def compute_daily_rollups_for_range(start_date, end_date) -> list:
    """Inclusive backfill over [start_date, end_date]. Used by the management command and by tests."""
    if start_date > end_date:
        raise ValueError("start_date must not be after end_date")

    results = []
    current = start_date
    while current <= end_date:
        results.append(compute_daily_rollup(current))
        current += timedelta(days=1)
    return results


def _closed_days_range(days: int):
    """The [start_date, end_date] of the most recent `days` fully-closed UTC days (ending yesterday)."""
    today = timezone.localdate()
    end_date = today - timedelta(days=1)
    start_date = end_date - timedelta(days=days - 1)
    return start_date, end_date


def get_daily_rollups(*, days: int):
    """Queryset of rollup rows for the trend-list endpoints, newest first (the model's default ordering)."""
    start_date, end_date = _closed_days_range(days)
    return DailyPlatformRollup.objects.filter(date__gte=start_date, date__lte=end_date)


def get_operations_summary(*, days: int) -> dict:
    """
    Window totals for ride/dispatch health, summed from the rollup table
    rather than scanned live from Ride/DispatchOffer — the whole point of
    having the table. Rates are computed from summed counts
    (accepted-over-window / sent-over-window), not by averaging each
    day's own rate, which would silently over-weight low-volume days.
    """
    start_date, end_date = _closed_days_range(days)
    qs = DailyPlatformRollup.objects.filter(date__gte=start_date, date__lte=end_date)
    totals = qs.aggregate(
        rides_requested=Sum("rides_requested"),
        rides_completed=Sum("rides_completed"),
        rides_cancelled=Sum("rides_cancelled"),
        rides_no_driver_found=Sum("rides_no_driver_found"),
        active_driver_days=Sum("active_drivers"),
        offers_sent=Sum("offers_sent"),
        offers_accepted=Sum("offers_accepted"),
        offers_expired=Sum("offers_expired"),
        offers_declined=Sum("offers_declined"),
        payment_failures=Sum("payment_failures"),
    )
    rides_requested = totals["rides_requested"] or 0
    offers_sent = totals["offers_sent"] or 0

    return {
        "window_days": days,
        "start_date": start_date,
        "end_date": end_date,
        "days_available": qs.count(),
        "rides_requested": rides_requested,
        "rides_completed": totals["rides_completed"] or 0,
        "rides_cancelled": totals["rides_cancelled"] or 0,
        "rides_no_driver_found": totals["rides_no_driver_found"] or 0,
        # Sum of each day's distinct active-driver count — a driver active
        # on 5 different days contributes 5 "driver-days", not 1. Reporting
        # true distinct drivers over an arbitrary window would need a live
        # query over the window (exactly what the rollup avoids), so this
        # is a driver-days figure, named as such rather than as a count
        # that looks like, but isn't, "distinct active drivers this month".
        "active_driver_days": totals["active_driver_days"] or 0,
        "offers_sent": offers_sent,
        "offers_accepted": totals["offers_accepted"] or 0,
        "offers_expired": totals["offers_expired"] or 0,
        "offers_declined": totals["offers_declined"] or 0,
        "offer_acceptance_rate": round((totals["offers_accepted"] or 0) / offers_sent, 3) if offers_sent else None,
        "no_driver_found_rate": (
            round((totals["rides_no_driver_found"] or 0) / rides_requested, 3) if rides_requested else None
        ),
        "payment_failures": totals["payment_failures"] or 0,
    }


def get_revenue_summary(*, days: int) -> dict:
    """Window financial totals plus a per-vehicle-type breakdown, both summed from the rollup tables."""
    start_date, end_date = _closed_days_range(days)
    qs = DailyPlatformRollup.objects.filter(date__gte=start_date, date__lte=end_date)
    totals = qs.aggregate(
        gross=Sum("gross_revenue"),
        commission=Sum("platform_commission"),
        driver_share=Sum("driver_earnings"),
        ride_count=Sum("rides_with_earning"),
    )
    gross_revenue = totals["gross"] or ZERO
    ride_count = totals["ride_count"] or 0

    by_vehicle_type = list(
        DailyVehicleTypeRollup.objects.filter(date__gte=start_date, date__lte=end_date)
        .values("vehicle_type__name")
        .annotate(ride_count=Sum("ride_count"), gross=Sum("gross_revenue"), commission=Sum("platform_commission"))
        .order_by("-gross")
    )

    return {
        "window_days": days,
        "start_date": start_date,
        "end_date": end_date,
        "days_available": qs.count(),
        "ride_count": ride_count,
        "gross_revenue": gross_revenue,
        "platform_commission": totals["commission"] or ZERO,
        "driver_earnings": totals["driver_share"] or ZERO,
        "average_fare": quantize_money(gross_revenue / ride_count) if ride_count else None,
        "by_vehicle_type": by_vehicle_type,
    }
