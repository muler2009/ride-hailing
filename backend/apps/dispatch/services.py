"""
Dispatch proposes; Ride Management decides. This module finds candidate
drivers and creates/tracks DispatchOffer rows, but it never writes
Ride.status directly — the actual state transition when a driver wins an
offer happens in apps/rides/services.py (accept_ride), which is the only
place any ride's status is written, matching the architecture doc's
domain-boundary note: "Dispatch reads from Location Tracking... but never
writes ride state directly."

Offers are sent to several candidates at once (not strictly sequential) —
the first to accept wins, and every other pending offer for that ride is
superseded. This is the scenario the architecture doc's "prevent multiple
drivers from accepting the same ride" note is about, and it's why
apps/rides/services.py:accept_ride does its work inside a
select_for_update-locked transaction.
"""
from datetime import timedelta

from django.utils import timezone

from apps.locations import geo
from apps.dispatch.models import DispatchOffer, DispatchOfferStatus

DEFAULT_SEARCH_RADIUS_KM = 5
DEFAULT_MAX_OFFERS = 3
OFFER_TTL_SECONDS = 15
MAX_SEARCH_SECONDS = 120  # beyond this with no candidates at all, the ride is marked NO_DRIVER_FOUND


def find_candidate_drivers(ride, radius_km: float = DEFAULT_SEARCH_RADIUS_KM, limit: int = 20):
    """
    Returns a ranked list of (Driver, distance_km) tuples: online, approved,
    not already offered this ride, not currently on another active ride,
    with an active vehicle matching the requested type, within radius_km of
    pickup and with a location fresh enough to trust. Nearest first, with
    rating and acceptance-rate as tie-breakers.
    """
    from apps.drivers.models import Driver, DriverApprovalStatus
    from apps.rides.models import TERMINAL_STATUSES, Ride

    nearby = geo.find_nearby_driver_ids(
        latitude=float(ride.pickup_latitude), longitude=float(ride.pickup_longitude),
        radius_km=radius_km, count=limit,
    )
    if not nearby:
        return []

    fresh_nearby = [(driver_id, distance) for driver_id, distance in nearby if geo.is_location_fresh(driver_id)]
    if not fresh_nearby:
        return []

    already_offered_ids = set(
        str(d) for d in DispatchOffer.objects.filter(ride=ride).values_list("driver_id", flat=True)
    )
    busy_driver_ids = set(
        str(d) for d in Ride.objects.exclude(status__in=TERMINAL_STATUSES)
        .exclude(driver__isnull=True).values_list("driver_id", flat=True)
    )

    distance_by_id = {driver_id: distance for driver_id, distance in fresh_nearby}
    eligible_ids = [
        driver_id for driver_id in distance_by_id
        if driver_id not in already_offered_ids and driver_id not in busy_driver_ids
    ]
    if not eligible_ids:
        return []

    drivers = Driver.objects.filter(
        id__in=eligible_ids,
        is_available=True,
        approval_status=DriverApprovalStatus.APPROVED,
        vehicles__is_active=True,
        vehicles__vehicle_type=ride.vehicle_type,
    ).distinct()

    candidates = [(driver, distance_by_id[str(driver.id)]) for driver in drivers]

    def sort_key(item):
        driver, distance = item
        rating_score = -float(driver.average_rating) if driver.average_rating is not None else 0.0
        acceptance = driver.acceptance_rate
        acceptance_score = -acceptance if acceptance is not None else 0.0
        return (distance, rating_score, acceptance_score)

    candidates.sort(key=sort_key)
    return candidates


def create_offers(ride, candidates, max_offers: int = DEFAULT_MAX_OFFERS, ttl_seconds: int = OFFER_TTL_SECONDS):
    """Sends simultaneous offers to the top `max_offers` candidates."""
    expires_at = timezone.now() + timedelta(seconds=ttl_seconds)
    offers = []
    for driver, distance in candidates[:max_offers]:
        offer = DispatchOffer.objects.create(
            ride=ride, driver=driver, distance_km=distance, expires_at=expires_at
        )
        driver.total_offers_sent += 1
        driver.save(update_fields=["total_offers_sent"])
        offers.append(offer)
    return offers


def dispatch_ride(ride, radius_km: float = DEFAULT_SEARCH_RADIUS_KM, max_offers: int = DEFAULT_MAX_OFFERS):
    """Convenience wrapper: find candidates for this one ride and offer to the best of them."""
    candidates = find_candidate_drivers(ride, radius_km=radius_km)
    if not candidates:
        return []
    return create_offers(ride, candidates, max_offers=max_offers)


# ---------------------------------------------------------------------------
# Offer bookkeeping — used by apps/rides/services.py's accept_ride/decline_ride
# to check "does this driver have a live offer on this ride" and to record
# the outcome, without those functions needing to know how offers were
# generated.
# ---------------------------------------------------------------------------

def get_pending_offer(ride_id, driver_id):
    return DispatchOffer.objects.filter(
        ride_id=ride_id, driver_id=driver_id, status=DispatchOfferStatus.PENDING, expires_at__gt=timezone.now()
    ).first()


def mark_offer_responded(offer: DispatchOffer, new_status: str) -> None:
    offer.status = new_status
    offer.responded_at = timezone.now()
    offer.save(update_fields=["status", "responded_at"])


def supersede_other_pending_offers(ride_id, winning_driver_id) -> int:
    return (
        DispatchOffer.objects.filter(ride_id=ride_id, status=DispatchOfferStatus.PENDING)
        .exclude(driver_id=winning_driver_id)
        .update(status=DispatchOfferStatus.SUPERSEDED, responded_at=timezone.now())
    )


def record_driver_accepted(driver) -> None:
    driver.total_offers_accepted += 1
    driver.save(update_fields=["total_offers_accepted"])


def supersede_all_pending_offers(ride_id) -> int:
    """Used when an admin manually assigns a driver, short-circuiting automatic dispatch."""
    return DispatchOffer.objects.filter(ride_id=ride_id, status=DispatchOfferStatus.PENDING).update(
        status=DispatchOfferStatus.SUPERSEDED, responded_at=timezone.now()
    )


# ---------------------------------------------------------------------------
# Periodic cycle — stands in for what a Celery beat task would run every
# few seconds in production (expire overdue offers, dispatch/redispatch
# searching rides, give up on rides nobody will ever reach).
# ---------------------------------------------------------------------------

def expire_stale_offers() -> int:
    return DispatchOffer.objects.filter(
        status=DispatchOfferStatus.PENDING, expires_at__lte=timezone.now()
    ).update(status=DispatchOfferStatus.EXPIRED, responded_at=timezone.now())


def run_dispatch_cycle() -> dict:
    from apps.rides.models import Ride, RideStatus
    from apps.rides.services import mark_no_driver_found

    expired_count = expire_stale_offers()
    results = {"expired_offers": expired_count, "dispatched": 0, "no_driver_found": 0, "still_searching": 0}

    for ride in Ride.objects.filter(status=RideStatus.SEARCHING_DRIVER):
        if DispatchOffer.objects.filter(ride=ride, status=DispatchOfferStatus.PENDING).exists():
            results["still_searching"] += 1
            continue

        offers = dispatch_ride(ride)
        if offers:
            results["dispatched"] += 1
            continue

        age_seconds = (timezone.now() - ride.created_at).total_seconds()
        if age_seconds > MAX_SEARCH_SECONDS:
            mark_no_driver_found(ride.id, changed_by=None)
            results["no_driver_found"] += 1
        else:
            results["still_searching"] += 1

    return results
