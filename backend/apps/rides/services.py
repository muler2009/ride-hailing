from django.db import transaction
from django.utils import timezone

from apps.rides.models import (
    DRIVER_CANCELLABLE_STATUSES,
    RIDER_CANCELLABLE_STATUSES,
    RIDER_CANCELLATION_FEE_STATUSES,
    Ride,
    RideStatus,
    RideStatusHistory,
)


class InvalidRideTransition(Exception):
    pass


class RidePermissionError(Exception):
    """Raised when the caller isn't the right party to act on this ride (not a state error)."""


# Explicit transition table: {current_status: {allowed_next_status, ...}}.
# This is the ONLY place ride transitions are enumerated — every service
# function below checks against it via _transition(), and no view or
# serializer ever sets Ride.status directly. Mirrors the driver approval
# state machine's pattern in apps/drivers/services.py.
#
# PAYMENT_PENDING -> PAID and PAID -> RATED are listed for completeness
# with the architecture doc's full state diagram, but are not reachable
# through any endpoint yet — Payments (Phase 8) and Ratings (Phase 11)
# aren't built. TRIP_COMPLETED auto-advances to PAYMENT_PENDING as a
# system transition and stops there until Phase 8 adds the endpoint that
# moves it to PAID.
_ALLOWED_TRANSITIONS = {
    RideStatus.REQUESTED: {RideStatus.SEARCHING_DRIVER},
    RideStatus.SEARCHING_DRIVER: {
        RideStatus.DRIVER_ASSIGNED,
        RideStatus.NO_DRIVER_FOUND,
        RideStatus.CANCELLED_BY_RIDER,
    },
    RideStatus.DRIVER_ASSIGNED: {
        RideStatus.DRIVER_ACCEPTED,
        RideStatus.SEARCHING_DRIVER,  # driver declined
        RideStatus.CANCELLED_BY_RIDER,
        RideStatus.CANCELLED_BY_DRIVER,
    },
    RideStatus.DRIVER_ACCEPTED: {
        RideStatus.DRIVER_ARRIVING,
        RideStatus.CANCELLED_BY_RIDER,
        RideStatus.CANCELLED_BY_DRIVER,
    },
    RideStatus.DRIVER_ARRIVING: {
        RideStatus.DRIVER_ARRIVED,
        RideStatus.CANCELLED_BY_RIDER,
        RideStatus.CANCELLED_BY_DRIVER,
    },
    RideStatus.DRIVER_ARRIVED: {
        RideStatus.TRIP_STARTED,
        RideStatus.CANCELLED_BY_RIDER,
        RideStatus.CANCELLED_BY_DRIVER,
        RideStatus.EXPIRED,  # rider no-show
    },
    RideStatus.TRIP_STARTED: {RideStatus.TRIP_COMPLETED},
    RideStatus.TRIP_COMPLETED: {RideStatus.PAYMENT_PENDING},
    RideStatus.PAYMENT_PENDING: {RideStatus.PAID},
    RideStatus.PAID: {RideStatus.RATED},
}


def _transition(ride: Ride, new_status: str, *, changed_by=None, note: str = "") -> None:
    allowed = _ALLOWED_TRANSITIONS.get(ride.status, set())
    if new_status not in allowed:
        raise InvalidRideTransition(f"Cannot move ride from {ride.status} to {new_status}.")

    old_status = ride.status
    ride.status = new_status
    ride.version += 1
    RideStatusHistory.objects.create(
        ride=ride, from_status=old_status, to_status=new_status, changed_by=changed_by, note=note
    )

    # Capture plain values now rather than closing over the live `ride`
    # object: a single request can call _transition twice (e.g.
    # complete_trip chains TRIP_COMPLETED then PAYMENT_PENDING), and by the
    # time transaction.on_commit's callbacks actually run, `ride.status`
    # would already reflect the LAST transition for both callbacks if we
    # read it lazily — each broadcast needs to report the status it was
    # scheduled for, not whatever the ride ended up at.
    ride_id, driver_id = ride.id, ride.driver_id

    def _broadcast():
        from apps.locations.services import broadcast_to_ride

        broadcast_to_ride(
            ride_id,
            {"type": "status.update", "status": new_status, "driver_id": str(driver_id) if driver_id else None},
        )

    transaction.on_commit(_broadcast)


def _lock(ride_id) -> Ride:
    """Row-locks the ride for the duration of the enclosing transaction."""
    return Ride.objects.select_for_update().get(pk=ride_id)


def _notify_ride_event(ride: Ride, event_type: str, title: str, body: str) -> None:
    """
    Best-effort, same tolerance pattern as pricing/dispatch/maps elsewhere
    in this file: notification delivery (apps/notifications, Phase 10) is
    a separate bounded context, and its unavailability should never
    unwind a ride transition that has already genuinely happened.
    Delivery itself is asynchronous (a Celery task) — this call only
    creates the Notification row(s) and enqueues them, it doesn't send
    anything inline.
    """
    try:
        from apps.notifications.services import notify

        notify(
            event_type=event_type,
            title=title,
            body=body,
            recipient_user=ride.rider,
            guest_phone_number=ride.guest_phone_number or "",
            ride=ride,
        )
    except Exception:
        import logging

        logging.getLogger(__name__).exception(
            "Notification dispatch failed for ride %s event %s", ride.id, event_type
        )


# ---------------------------------------------------------------------------
# Creation
# ---------------------------------------------------------------------------

@transaction.atomic
def create_ride(
    *,
    vehicle_type,
    pickup_address: str,
    pickup_latitude,
    pickup_longitude,
    destination_address: str,
    destination_latitude,
    destination_longitude,
    rider=None,
    guest_phone_number: str = "",
    guest_name: str = "",
) -> Ride:
    """
    Creates a ride for either a registered rider or a phone-only guest —
    exactly one of `rider` / `guest_phone_number` must be given (the
    serializer validates this against the request context; this is a
    defense-in-depth re-check, matching the DB CheckConstraint on Ride).

    No service-area validation, routing, or fare estimation happens yet
    (Maps & Pricing are Phases 6-7) — the ride is created directly in
    REQUESTED and immediately auto-advances to SEARCHING_DRIVER, standing
    in for what will later be a real validation step.
    """
    if bool(rider) == bool(guest_phone_number):
        raise ValueError("Exactly one of rider or guest_phone_number must be provided.")

    ride = Ride.objects.create(
        rider=rider,
        guest_phone_number=guest_phone_number or None,
        guest_name=guest_name,
        vehicle_type=vehicle_type,
        pickup_address=pickup_address,
        pickup_latitude=pickup_latitude,
        pickup_longitude=pickup_longitude,
        destination_address=destination_address,
        destination_latitude=destination_latitude,
        destination_longitude=destination_longitude,
        status=RideStatus.REQUESTED,
    )
    RideStatusHistory.objects.create(ride=ride, from_status="", to_status=RideStatus.REQUESTED, changed_by=rider)

    _transition(ride, RideStatus.SEARCHING_DRIVER, changed_by=None, note="Automatic — no real dispatch yet (Phase 4).")
    ride.save()

    _notify_ride_event(
        ride,
        "RIDE_REQUESTED",
        "Finding your driver",
        "We're looking for a nearby driver for your trip.",
    )

    return ride


# ---------------------------------------------------------------------------
# Manual dispatch override — an admin/ops action that short-circuits
# whatever automatic dispatch (apps/dispatch) is doing for this ride. Still
# useful post-Phase-4 for edge cases (VIP handling, a ride stuck with no
# eligible candidates, support intervention).
# ---------------------------------------------------------------------------

@transaction.atomic
def assign_driver(ride_id, driver, assigned_by) -> Ride:
    from apps.dispatch.services import supersede_all_pending_offers
    from apps.drivers.models import DriverApprovalStatus

    ride = _lock(ride_id)
    if driver.approval_status != DriverApprovalStatus.APPROVED:
        raise ValueError("Only an approved driver may be assigned to a ride.")

    ride.driver = driver
    ride.driver_assigned_at = timezone.now()
    _transition(ride, RideStatus.DRIVER_ASSIGNED, changed_by=assigned_by, note="Manually assigned by an operator, overriding automatic dispatch.")
    ride.save()
    supersede_all_pending_offers(ride.id)

    from apps.admin_api.audit import record_audit

    record_audit(
        actor=assigned_by, action="RIDE_DRIVER_ASSIGNED", target_type="Ride", target_id=ride.id,
        changes={"driver_id": str(driver.id)},
    )

    return ride


@transaction.atomic
def mark_no_driver_found(ride_id, changed_by=None) -> Ride:
    ride = _lock(ride_id)
    _transition(ride, RideStatus.NO_DRIVER_FOUND, changed_by=changed_by)
    ride.save()
    return ride


# ---------------------------------------------------------------------------
# Driver-side lifecycle. Each function verifies the calling driver is the
# one assigned to this ride — ownership, not just a valid state transition.
# ---------------------------------------------------------------------------

def _require_assigned_driver(ride: Ride, driver) -> None:
    if ride.driver_id != driver.id:
        raise RidePermissionError("You are not the driver assigned to this ride.")


@transaction.atomic
def accept_ride(ride_id, driver) -> Ride:
    """
    Handles two distinct paths, both ending at DRIVER_ACCEPTED:

      1. Admin-assigned (Phase 3 path, still used for manual overrides):
         ride.driver is already this driver and status is DRIVER_ASSIGNED
         — just complete that single transition.

      2. Automatic dispatch (Phase 4): the ride is still SEARCHING_DRIVER
         and multiple drivers may have a live offer on it simultaneously.
         This driver's acceptance both assigns AND accepts in one atomic,
         row-locked step — whichever driver's request reaches here first
         wins; every other pending offer for this ride is superseded
         immediately after, so a losing driver's own accept attempt (if it
         arrives microseconds later) finds the ride no longer
         SEARCHING_DRIVER and fails cleanly rather than racing further.
    """
    from apps.dispatch.services import (
        get_pending_offer,
        mark_offer_responded,
        record_driver_accepted,
        supersede_other_pending_offers,
    )
    from apps.dispatch.models import DispatchOfferStatus

    ride = _lock(ride_id)

    if ride.driver_id == driver.id and ride.status == RideStatus.DRIVER_ASSIGNED:
        ride.driver_accepted_at = timezone.now()
        _transition(ride, RideStatus.DRIVER_ACCEPTED, changed_by=driver.user)
        ride.save()
        _notify_ride_event(
            ride, "RIDE_DRIVER_ASSIGNED", "Driver assigned",
            f"{driver.user.full_name or driver.user.email} is on the way.",
        )
        return ride

    if ride.status == RideStatus.SEARCHING_DRIVER:
        offer = get_pending_offer(ride.id, driver.id)
        if offer is None:
            raise RidePermissionError("You do not have an active offer for this ride.")

        ride.driver = driver
        ride.driver_assigned_at = timezone.now()
        _transition(ride, RideStatus.DRIVER_ASSIGNED, changed_by=None, note="Automatic dispatch: driver won the offer.")
        ride.driver_accepted_at = timezone.now()
        _transition(ride, RideStatus.DRIVER_ACCEPTED, changed_by=driver.user)
        ride.save()

        mark_offer_responded(offer, DispatchOfferStatus.ACCEPTED)
        supersede_other_pending_offers(ride.id, winning_driver_id=driver.id)
        record_driver_accepted(driver)
        _notify_ride_event(
            ride, "RIDE_DRIVER_ASSIGNED", "Driver assigned",
            f"{driver.user.full_name or driver.user.email} is on the way.",
        )
        return ride

    raise RidePermissionError("You are not the driver assigned to this ride.")


@transaction.atomic
def decline_ride(ride_id, driver) -> Ride:
    """
    Mirrors accept_ride's two paths. Declining an automatic-dispatch offer
    only updates that offer's own status (DECLINED) — the ride itself stays
    SEARCHING_DRIVER, since other candidates may still have a pending offer
    or a future dispatch cycle will try someone else. Declining an
    admin-assigned ride (no dispatch offer involved) returns the ride to
    SEARCHING_DRIVER directly, as in Phase 3.
    """
    from apps.dispatch.services import get_pending_offer, mark_offer_responded
    from apps.dispatch.models import DispatchOfferStatus

    ride = _lock(ride_id)

    if ride.driver_id == driver.id and ride.status == RideStatus.DRIVER_ASSIGNED:
        ride.driver = None
        ride.driver_assigned_at = None
        _transition(ride, RideStatus.SEARCHING_DRIVER, changed_by=driver.user, note="Declined by driver.")
        ride.save()
        return ride

    if ride.status == RideStatus.SEARCHING_DRIVER:
        offer = get_pending_offer(ride.id, driver.id)
        if offer is None:
            raise RidePermissionError("You do not have an active offer for this ride.")
        mark_offer_responded(offer, DispatchOfferStatus.DECLINED)
        return ride

    raise RidePermissionError("You are not the driver assigned to this ride.")


@transaction.atomic
def mark_arriving(ride_id, driver) -> Ride:
    ride = _lock(ride_id)
    _require_assigned_driver(ride, driver)
    _transition(ride, RideStatus.DRIVER_ARRIVING, changed_by=driver.user)
    ride.save()
    _notify_ride_event(ride, "RIDE_DRIVER_ARRIVING", "Driver on the way", "Your driver is heading to the pickup point.")
    return ride


@transaction.atomic
def mark_arrived(ride_id, driver) -> Ride:
    ride = _lock(ride_id)
    _require_assigned_driver(ride, driver)
    _transition(ride, RideStatus.DRIVER_ARRIVED, changed_by=driver.user)
    ride.save()
    _notify_ride_event(ride, "RIDE_DRIVER_ARRIVED", "Driver has arrived", "Your driver is waiting at the pickup point.")
    return ride


@transaction.atomic
def start_trip(ride_id, driver) -> Ride:
    ride = _lock(ride_id)
    _require_assigned_driver(ride, driver)
    ride.trip_started_at = timezone.now()
    _transition(ride, RideStatus.TRIP_STARTED, changed_by=driver.user)
    ride.save()
    _notify_ride_event(ride, "RIDE_STARTED", "Trip started", "Your trip is now underway.")
    return ride


@transaction.atomic
def complete_trip(ride_id, driver) -> Ride:
    """
    Completes the trip and auto-advances straight to PAYMENT_PENDING — a
    system transition per the architecture doc, recorded as its own
    history row even though it happens in the same request, since the
    audit trail should reflect the state diagram exactly, not just the API
    calls that triggered it.
    """
    ride = _lock(ride_id)
    _require_assigned_driver(ride, driver)
    ride.trip_completed_at = timezone.now()
    _transition(ride, RideStatus.TRIP_COMPLETED, changed_by=driver.user)
    _transition(ride, RideStatus.PAYMENT_PENDING, changed_by=None, note="Automatic — Payments not built yet (Phase 8).")
    ride.save()

    # Best-effort: pricing is its own bounded context (see apps/pricing).
    # A missing pricing rule for this vehicle type shouldn't block trip
    # completion itself — Payments (Phase 8) will need a real fare to
    # charge against, but that's Phase 8's concern to enforce, not this
    # transition's.
    try:
        from apps.pricing.services import finalize_fare_for_ride

        finalize_fare_for_ride(ride)
    except Exception:
        import logging

        logging.getLogger(__name__).exception("Final fare calculation failed for ride %s", ride.id)

    _notify_ride_event(ride, "RIDE_COMPLETED", "Trip completed", "You've arrived — thanks for riding with us.")

    return ride


@transaction.atomic
def expire_ride(ride_id, changed_by=None) -> Ride:
    """Rider no-show at DRIVER_ARRIVED. Manually triggered for now — no scheduler exists yet."""
    ride = _lock(ride_id)
    _transition(ride, RideStatus.EXPIRED, changed_by=changed_by)
    ride.save()
    return ride


@transaction.atomic
def mark_paid(ride_id) -> Ride:
    """
    PAYMENT_PENDING -> PAID. Called exclusively from apps/payments/services.py
    once a payment is genuinely captured (a verified webhook, a driver
    confirming cash, ...) — never from anything a client asserts directly.
    Idempotent: if the ride is already PAID (e.g. a duplicate webhook
    delivery got this far — apps/payments' own idempotency guard should
    normally prevent that, but this is a harmless no-op either way rather
    than a confusing error), it's simply returned as-is.
    """
    ride = _lock(ride_id)
    if ride.status == RideStatus.PAID:
        return ride
    _transition(ride, RideStatus.PAID, changed_by=None, note="Payment captured.")
    ride.save()

    # Best-effort, same tolerance pattern as pricing/dispatch elsewhere in
    # this file: a missing final fare or a driver-earnings hiccup
    # shouldn't unwind the payment itself, which has already genuinely
    # happened by this point.
    try:
        from apps.earnings.services import credit_driver_earning

        credit_driver_earning(ride)
    except Exception:
        import logging

        logging.getLogger(__name__).exception("Driver earning credit failed for ride %s", ride.id)

    return ride


@transaction.atomic
def mark_rated(ride_id) -> Ride:
    """
    PAID -> RATED. Called exclusively from apps/ratings/services.py once a
    rating is genuinely recorded. Fires on the FIRST rating for a ride
    (either direction) — RATED means "at least one party has rated," since
    waiting for both would leave rides permanently stuck at PAID whenever
    one side never bothers.

    Idempotent: already-RATED is returned as-is rather than raising, since
    the second direction's rating legitimately arrives after the
    transition has already happened.
    """
    ride = _lock(ride_id)
    if ride.status == RideStatus.RATED:
        return ride
    _transition(ride, RideStatus.RATED, changed_by=None, note="Rating submitted.")
    ride.save()
    return ride


# ---------------------------------------------------------------------------
# Cancellation — either party, while the ride is still in a cancellable
# state for them specifically (a driver can't cancel before being assigned;
# a rider can cancel any time up through DRIVER_ARRIVED).
# ---------------------------------------------------------------------------

@transaction.atomic
def cancel_by_rider(ride_id, *, rider=None, guest_phone_number=None, reason: str = "") -> Ride:
    ride = _lock(ride_id)

    if ride.rider_id is not None:
        if rider is None or ride.rider_id != rider.id:
            raise RidePermissionError("You are not the rider on this ride.")
    else:
        if not guest_phone_number or ride.guest_phone_number != guest_phone_number:
            raise RidePermissionError("This ride does not belong to this guest.")

    if ride.status not in RIDER_CANCELLABLE_STATUSES:
        raise InvalidRideTransition(f"Ride cannot be cancelled by the rider from status {ride.status}.")

    status_before_cancellation = ride.status
    ride.cancelled_at = timezone.now()
    ride.cancellation_reason = reason
    _transition(ride, RideStatus.CANCELLED_BY_RIDER, changed_by=rider, note=reason)
    ride.save()

    # Best-effort, same tolerance as elsewhere: a missing pricing rule
    # shouldn't prevent the cancellation itself from going through.
    if status_before_cancellation in RIDER_CANCELLATION_FEE_STATUSES:
        try:
            from apps.pricing.services import apply_cancellation_fee

            apply_cancellation_fee(ride)
        except Exception:
            import logging

            logging.getLogger(__name__).exception("Cancellation fee calculation failed for ride %s", ride.id)

    return ride


@transaction.atomic
def cancel_by_driver(ride_id, driver, reason: str = "") -> Ride:
    ride = _lock(ride_id)
    _require_assigned_driver(ride, driver)

    if ride.status not in DRIVER_CANCELLABLE_STATUSES:
        raise InvalidRideTransition(f"Ride cannot be cancelled by the driver from status {ride.status}.")

    ride.cancelled_at = timezone.now()
    ride.cancellation_reason = reason
    _transition(ride, RideStatus.CANCELLED_BY_DRIVER, changed_by=driver.user, note=reason)
    ride.save()
    return ride
