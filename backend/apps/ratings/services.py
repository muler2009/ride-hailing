"""
Submitting a rating does three things, in one transaction:
  1. records the Rating row (one per ride per direction),
  2. recomputes the ratee's aggregate average, and
  3. advances the ride PAID -> RATED — the last transition in the Phase 3
     state machine table that had no endpoint behind it until now.

On (3): the architecture doc's state diagram treats RATED as terminal, and
either party rating is enough to reach it — so the transition fires on the
FIRST rating for a ride, and the second one (the other direction) is
accepted without attempting the transition again. This means RATED means
"at least one party has rated," not "both have," which is the only reading
compatible with a rider who never rates and a driver who does.

On (2): Driver.average_rating has existed since Phase 4, where dispatch
ranking reads it as a tie-breaker — but nothing has ever written it until
now. It's recomputed from scratch (an aggregate over all that driver's
received ratings) rather than incrementally adjusted, because an exact
recompute is cheap at this scale and an incremental running average is
exactly the kind of derived-state drift the ledger discipline elsewhere
in this codebase avoids.
"""
from django.db import transaction
from django.db.models import Avg

from apps.ratings.models import Rating, RatingDirection
from apps.rides.models import RideStatus


class RatingError(Exception):
    pass


@transaction.atomic
def submit_rating(ride, *, direction: str, score: int, review: str = "", rater=None, guest_phone_number: str = "") -> Rating:
    """
    `rater` is the authenticated submitter, or None with a
    `guest_phone_number` for a guest rider rating via tracking_token.
    Authorization (is this caller actually a party to this ride?) is
    checked here, not left to the view — the same pattern used for ride
    cancellation in Phase 3.
    """
    if ride.status not in (RideStatus.PAID, RideStatus.RATED):
        raise RatingError(f"A ride can only be rated once it has been paid (current status: {ride.status}).")

    if Rating.objects.filter(ride=ride, direction=direction).exists():
        raise RatingError("This ride has already been rated in this direction.")

    if direction == RatingDirection.RIDER_TO_DRIVER:
        _authorize_rider(ride, rater, guest_phone_number)
        if ride.driver_id is None:
            raise RatingError("This ride has no driver to rate.")
        ratee = ride.driver.user
    elif direction == RatingDirection.DRIVER_TO_RIDER:
        _authorize_driver(ride, rater)
        if ride.rider_id is None:
            raise RatingError("This ride's rider was a guest and has no account to rate.")
        ratee = ride.rider
    else:
        raise RatingError(f"Unknown rating direction: {direction}")

    rating = Rating.objects.create(
        ride=ride,
        direction=direction,
        score=score,
        review=review,
        rater=rater,
        ratee=ratee,
    )

    if direction == RatingDirection.RIDER_TO_DRIVER:
        recompute_driver_average(ride.driver)

    if ride.status == RideStatus.PAID:
        from apps.rides.services import mark_rated

        mark_rated(ride.id)

    return rating


def _authorize_rider(ride, rater, guest_phone_number: str) -> None:
    if ride.rider_id is not None:
        if rater is None or ride.rider_id != rater.id:
            raise RatingError("You are not the rider on this ride.")
    else:
        if not guest_phone_number or ride.guest_phone_number != guest_phone_number:
            raise RatingError("This ride does not belong to this guest.")


def _authorize_driver(ride, rater) -> None:
    driver = getattr(rater, "driver_profile", None) if rater else None
    if driver is None or ride.driver_id != driver.id:
        raise RatingError("You are not the driver assigned to this ride.")


def recompute_driver_average(driver) -> None:
    """
    Recomputes Driver.average_rating from all ratings that driver has
    received. Called after every new RIDER_TO_DRIVER rating.
    """
    average = Rating.objects.filter(
        ratee=driver.user, direction=RatingDirection.RIDER_TO_DRIVER
    ).aggregate(avg=Avg("score"))["avg"]

    driver.average_rating = round(average, 2) if average is not None else None
    driver.save(update_fields=["average_rating"])


def get_rider_average(user):
    """Riders have no denormalized average field, so this is computed on read."""
    return Rating.objects.filter(
        ratee=user, direction=RatingDirection.DRIVER_TO_RIDER
    ).aggregate(avg=Avg("score"))["avg"]
