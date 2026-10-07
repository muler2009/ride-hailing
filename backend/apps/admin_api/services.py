"""
Read-only aggregation for the operator dashboard. No domain logic lives
here — every number is derived from tables other apps own, which is
exactly the architecture doc's framing of the Admin context ("No new
data — composes other contexts").

These are straightforward aggregate queries, appropriate at this scale.
At serious volume the ride/revenue rollups would move to materialized
views or a periodically-refreshed summary table (Phase 13's territory),
but computing them live keeps them exactly correct with no staleness
window, which matters more right now than shaving query time.
"""
from datetime import timedelta

from django.db.models import Avg, Count, Sum
from django.utils import timezone

from apps.drivers.models import Driver, DriverApprovalStatus
from apps.earnings.models import DriverEarning, Payout, PayoutStatus
from apps.payments.models import Payment, PaymentStatus
from apps.rides.models import TERMINAL_STATUSES, Ride, RideStatus

# Statuses representing a ride currently in flight — anything past
# request but not yet finished.
ACTIVE_RIDE_STATUSES = [
    RideStatus.REQUESTED,
    RideStatus.SEARCHING_DRIVER,
    RideStatus.DRIVER_ASSIGNED,
    RideStatus.DRIVER_ACCEPTED,
    RideStatus.DRIVER_ARRIVING,
    RideStatus.DRIVER_ARRIVED,
    RideStatus.TRIP_STARTED,
]


def get_dashboard_summary(*, since_hours: int = 24) -> dict:
    """Operational snapshot: what's happening right now, plus a recent window."""
    window_start = timezone.now() - timedelta(hours=since_hours)
    recent_rides = Ride.objects.filter(created_at__gte=window_start)

    completed_statuses = [RideStatus.TRIP_COMPLETED, RideStatus.PAYMENT_PENDING, RideStatus.PAID, RideStatus.RATED]
    cancelled_statuses = [RideStatus.CANCELLED_BY_RIDER, RideStatus.CANCELLED_BY_DRIVER]

    captured_payments = Payment.objects.filter(
        created_at__gte=window_start,
        status__in=[PaymentStatus.CAPTURED, PaymentStatus.PARTIALLY_REFUNDED],
    )
    earnings = DriverEarning.objects.filter(created_at__gte=window_start)

    return {
        "window_hours": since_hours,
        "active_rides": Ride.objects.filter(status__in=ACTIVE_RIDE_STATUSES).count(),
        "online_drivers": Driver.objects.filter(is_available=True, approval_status=DriverApprovalStatus.APPROVED).count(),
        "approved_drivers": Driver.objects.filter(approval_status=DriverApprovalStatus.APPROVED).count(),
        "drivers_pending_review": Driver.objects.filter(approval_status=DriverApprovalStatus.PENDING_REVIEW).count(),
        "rides_requested": recent_rides.count(),
        "rides_completed": recent_rides.filter(status__in=completed_statuses).count(),
        "rides_cancelled": recent_rides.filter(status__in=cancelled_statuses).count(),
        "rides_no_driver_found": recent_rides.filter(status=RideStatus.NO_DRIVER_FOUND).count(),
        "gross_revenue": captured_payments.aggregate(total=Sum("amount"))["total"] or 0,
        "platform_commission": earnings.aggregate(total=Sum("commission_amount"))["total"] or 0,
        "driver_earnings": earnings.aggregate(total=Sum("driver_earning_amount"))["total"] or 0,
        "payouts_pending": Payout.objects.filter(status=PayoutStatus.PENDING).count(),
        "payouts_pending_amount": Payout.objects.filter(status=PayoutStatus.PENDING).aggregate(total=Sum("amount"))["total"] or 0,
        "payment_failures": Payment.objects.filter(created_at__gte=window_start, status=PaymentStatus.FAILED).count(),
    }


def get_live_rides() -> list:
    """
    Every in-flight ride with its driver's current position, for the
    operator's live map. Positions come from Redis (apps/locations), not
    the database — the same ephemeral store dispatch queries, so the map
    shows where drivers actually are rather than the last sampled
    snapshot.
    """
    from apps.locations import geo

    rides = (
        Ride.objects.filter(status__in=ACTIVE_RIDE_STATUSES)
        .select_related("rider", "driver__user", "vehicle_type")
        .order_by("-created_at")
    )

    results = []
    for ride in rides:
        location = None
        if ride.driver_id is not None:
            try:
                location = geo.get_driver_location(ride.driver_id)
            except Exception:
                location = None  # Redis unavailable — the board still renders, just without live pins

        results.append(
            {
                "ride_id": str(ride.id),
                "status": ride.status,
                "rider": ride.rider.email if ride.rider_id else f"guest:{ride.guest_phone_number}",
                "driver": ride.driver.user.email if ride.driver_id else None,
                "vehicle_type": ride.vehicle_type.name,
                "pickup_address": ride.pickup_address,
                "destination_address": ride.destination_address,
                "driver_latitude": location[0] if location else None,
                "driver_longitude": location[1] if location else None,
                "created_at": ride.created_at,
            }
        )
    return results


def get_revenue_report(*, days: int = 30) -> dict:
    """Financial rollup over a window, plus a per-vehicle-type breakdown."""
    window_start = timezone.now() - timedelta(days=days)
    earnings = DriverEarning.objects.filter(created_at__gte=window_start)

    by_vehicle_type = list(
        Ride.objects.filter(created_at__gte=window_start, earning__isnull=False)
        .values("vehicle_type__name")
        .annotate(
            ride_count=Count("id"),
            gross=Sum("earning__fare_amount"),
            commission=Sum("earning__commission_amount"),
        )
        .order_by("-gross")
    )

    totals = earnings.aggregate(
        gross=Sum("fare_amount"),
        commission=Sum("commission_amount"),
        driver_share=Sum("driver_earning_amount"),
        average_fare=Avg("fare_amount"),
        ride_count=Count("id"),
    )

    return {
        "window_days": days,
        "ride_count": totals["ride_count"] or 0,
        "gross_revenue": totals["gross"] or 0,
        "platform_commission": totals["commission"] or 0,
        "driver_earnings": totals["driver_share"] or 0,
        "average_fare": round(totals["average_fare"], 2) if totals["average_fare"] is not None else None,
        "by_vehicle_type": by_vehicle_type,
    }


def get_dispatch_health(*, since_hours: int = 24) -> dict:
    """
    Dispatch-quality metrics from the architecture doc's observability
    list — acceptance rate and no-driver-found rate are the two that
    actually indicate whether matching is working.
    """
    from apps.dispatch.models import DispatchOffer, DispatchOfferStatus

    window_start = timezone.now() - timedelta(hours=since_hours)
    offers = DispatchOffer.objects.filter(created_at__gte=window_start)
    total_offers = offers.count()
    accepted = offers.filter(status=DispatchOfferStatus.ACCEPTED).count()

    recent_rides = Ride.objects.filter(created_at__gte=window_start)
    total_rides = recent_rides.count()
    no_driver = recent_rides.filter(status=RideStatus.NO_DRIVER_FOUND).count()

    return {
        "window_hours": since_hours,
        "offers_sent": total_offers,
        "offers_accepted": accepted,
        "offer_acceptance_rate": round(accepted / total_offers, 3) if total_offers else None,
        "offers_expired": offers.filter(status=DispatchOfferStatus.EXPIRED).count(),
        "offers_declined": offers.filter(status=DispatchOfferStatus.DECLINED).count(),
        "no_driver_found_rate": round(no_driver / total_rides, 3) if total_rides else None,
    }
