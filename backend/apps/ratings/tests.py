from decimal import Decimal

from django.test import TestCase
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from apps.dispatch.services import create_offers
from apps.drivers.services import apply_as_driver, approve_driver
from apps.payments.services import confirm_cash_payment, initiate_payment
from apps.pricing.services import estimate_fare_for_ride
from apps.ratings.models import Rating, RatingDirection
from apps.ratings.services import RatingError, get_rider_average, submit_rating
from apps.rides.models import RideStatus
from apps.rides.services import accept_ride, complete_trip, create_ride, mark_arrived, mark_arriving, start_trip
from apps.users.models import User
from apps.vehicles.models import VehicleType
from apps.vehicles.services import register_vehicle


def _ride_kwargs(**overrides):
    base = dict(
        vehicle_type=VehicleType.objects.get(name="Sedan"),
        pickup_address="A", pickup_latitude="9.0300", pickup_longitude="38.7400",
        destination_address="B", destination_latitude="9.0100", destination_longitude="38.7600",
    )
    base.update(overrides)
    return base


def _make_driver(tag):
    user = User.objects.create(email=f"rate-driver-{tag}@example.com", keycloak_id=f"kc-rate-driver-{tag}")
    admin = User.objects.create(email=f"rate-admin-{tag}@example.com", keycloak_id=f"kc-rate-admin-{tag}")
    driver = apply_as_driver(user, license_number=f"D-{tag}", license_expiry="2030-01-01")
    approve_driver(driver, admin)
    register_vehicle(
        driver, vehicle_type=VehicleType.objects.get(name="Sedan"),
        make="Toyota", model="Corolla", year=2022, license_plate=f"RATE-{tag}",
    )
    return driver


def _paid_ride(tag, *, guest=False):
    driver = _make_driver(tag)
    if guest:
        rider = None
        ride = create_ride(guest_phone_number=f"+1555000{tag:04d}", **_ride_kwargs())
    else:
        rider = User.objects.create(email=f"rate-rider-{tag}@example.com", keycloak_id=f"kc-rate-rider-{tag}")
        ride = create_ride(rider=rider, **_ride_kwargs())

    ride.estimated_distance_km = Decimal("5.0")
    ride.estimated_duration_minutes = Decimal("10.0")
    ride.save()
    estimate_fare_for_ride(ride)
    create_offers(ride, [(driver, 0.3)])
    accept_ride(ride.id, driver)
    mark_arriving(ride.id, driver)
    mark_arrived(ride.id, driver)
    start_trip(ride.id, driver)
    ride = complete_trip(ride.id, driver)

    initiate_payment(ride, method="CASH")
    ride.refresh_from_db()
    confirm_cash_payment(ride, driver)
    ride.refresh_from_db()
    return ride, rider, driver


class SubmitRatingTests(TestCase):
    def test_rider_can_rate_driver_after_payment(self):
        ride, rider, driver = _paid_ride(1)
        rating = submit_rating(ride, direction=RatingDirection.RIDER_TO_DRIVER, score=5, review="Great trip", rater=rider)

        self.assertEqual(rating.score, 5)
        self.assertEqual(rating.ratee, driver.user)
        self.assertEqual(rating.rater, rider)

    def test_rating_advances_ride_to_rated(self):
        """The Phase 3 state machine's last unreachable transition, finally reachable."""
        ride, rider, driver = _paid_ride(2)
        self.assertEqual(ride.status, RideStatus.PAID)

        submit_rating(ride, direction=RatingDirection.RIDER_TO_DRIVER, score=4, rater=rider)

        ride.refresh_from_db()
        self.assertEqual(ride.status, RideStatus.RATED)

    def test_second_direction_rating_is_accepted_after_already_rated(self):
        ride, rider, driver = _paid_ride(3)
        submit_rating(ride, direction=RatingDirection.RIDER_TO_DRIVER, score=5, rater=rider)
        ride.refresh_from_db()

        # Ride is already RATED — the driver's own rating must still be accepted.
        rating = submit_rating(ride, direction=RatingDirection.DRIVER_TO_RIDER, score=4, rater=driver.user)

        self.assertEqual(rating.direction, RatingDirection.DRIVER_TO_RIDER)
        self.assertEqual(Rating.objects.filter(ride=ride).count(), 2)

    def test_cannot_rate_the_same_direction_twice(self):
        ride, rider, driver = _paid_ride(4)
        submit_rating(ride, direction=RatingDirection.RIDER_TO_DRIVER, score=5, rater=rider)

        with self.assertRaises(RatingError):
            submit_rating(ride, direction=RatingDirection.RIDER_TO_DRIVER, score=1, rater=rider)

    def test_cannot_rate_before_payment(self):
        driver = _make_driver(5)
        rider = User.objects.create(email="early-rate@example.com", keycloak_id="kc-early-rate")
        ride = create_ride(rider=rider, **_ride_kwargs())

        with self.assertRaises(RatingError):
            submit_rating(ride, direction=RatingDirection.RIDER_TO_DRIVER, score=5, rater=rider)

    def test_non_party_cannot_rate(self):
        ride, rider, driver = _paid_ride(6)
        stranger = User.objects.create(email="stranger@example.com", keycloak_id="kc-stranger")

        with self.assertRaises(RatingError):
            submit_rating(ride, direction=RatingDirection.RIDER_TO_DRIVER, score=5, rater=stranger)

    def test_driver_cannot_rate_a_guest_rider(self):
        """A guest has no account to attach reputation to, so DRIVER_TO_RIDER is rejected."""
        ride, _rider, driver = _paid_ride(7, guest=True)

        with self.assertRaises(RatingError):
            submit_rating(ride, direction=RatingDirection.DRIVER_TO_RIDER, score=5, rater=driver.user)

    def test_guest_rider_can_rate_their_driver(self):
        ride, _rider, driver = _paid_ride(8, guest=True)
        rating = submit_rating(
            ride, direction=RatingDirection.RIDER_TO_DRIVER, score=5,
            guest_phone_number=ride.guest_phone_number,
        )
        self.assertIsNone(rating.rater)
        self.assertEqual(rating.ratee, driver.user)

    def test_guest_with_wrong_phone_cannot_rate(self):
        ride, _rider, driver = _paid_ride(9, guest=True)
        with self.assertRaises(RatingError):
            submit_rating(
                ride, direction=RatingDirection.RIDER_TO_DRIVER, score=5,
                guest_phone_number="+15559999999",
            )


class DriverAverageTests(TestCase):
    def test_driver_average_is_populated_after_first_rating(self):
        """Driver.average_rating has existed since Phase 4 but nothing wrote it until now."""
        ride, rider, driver = _paid_ride(10)
        self.assertIsNone(driver.average_rating)

        submit_rating(ride, direction=RatingDirection.RIDER_TO_DRIVER, score=4, rater=rider)

        driver.refresh_from_db()
        self.assertEqual(driver.average_rating, Decimal("4.00"))

    def test_average_recomputes_across_multiple_rides(self):
        ride1, rider1, driver = _paid_ride(11)
        submit_rating(ride1, direction=RatingDirection.RIDER_TO_DRIVER, score=5, rater=rider1)

        # A second ride for the same driver.
        rider2 = User.objects.create(email="rate-rider-11b@example.com", keycloak_id="kc-rate-rider-11b")
        ride2 = create_ride(rider=rider2, **_ride_kwargs())
        ride2.estimated_distance_km = Decimal("5.0")
        ride2.estimated_duration_minutes = Decimal("10.0")
        ride2.save()
        estimate_fare_for_ride(ride2)
        create_offers(ride2, [(driver, 0.3)])
        accept_ride(ride2.id, driver)
        mark_arriving(ride2.id, driver)
        mark_arrived(ride2.id, driver)
        start_trip(ride2.id, driver)
        complete_trip(ride2.id, driver)
        ride2.refresh_from_db()
        initiate_payment(ride2, method="CASH")
        ride2.refresh_from_db()
        confirm_cash_payment(ride2, driver)
        ride2.refresh_from_db()

        submit_rating(ride2, direction=RatingDirection.RIDER_TO_DRIVER, score=3, rater=rider2)

        driver.refresh_from_db()
        self.assertEqual(driver.average_rating, Decimal("4.00"))  # (5 + 3) / 2

    def test_driver_rating_a_rider_does_not_affect_driver_average(self):
        ride, rider, driver = _paid_ride(12)
        submit_rating(ride, direction=RatingDirection.DRIVER_TO_RIDER, score=1, rater=driver.user)

        driver.refresh_from_db()
        self.assertIsNone(driver.average_rating)

    def test_rider_average_is_computed_on_read(self):
        ride, rider, driver = _paid_ride(13)
        submit_rating(ride, direction=RatingDirection.DRIVER_TO_RIDER, score=5, rater=driver.user)

        self.assertEqual(get_rider_average(rider), 5.0)


class RatingApiTests(APITestCase):
    def test_rider_submits_rating_over_the_api(self):
        ride, rider, driver = _paid_ride(20)
        self.client.force_authenticate(user=rider)

        response = self.client.post(
            reverse("ride_ratings", args=[ride.id]), {"score": 5, "review": "Excellent"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["direction"], "RIDER_TO_DRIVER")

    def test_direction_is_derived_from_caller_not_request_body(self):
        """A driver calling the same endpoint gets DRIVER_TO_RIDER — they can't submit 'as' the rider."""
        ride, rider, driver = _paid_ride(21)
        self.client.force_authenticate(user=driver.user)

        response = self.client.post(
            reverse("ride_ratings", args=[ride.id]),
            {"score": 4, "direction": "RIDER_TO_DRIVER"},  # attempted override, must be ignored
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["direction"], "DRIVER_TO_RIDER")

    def test_stranger_gets_403(self):
        ride, rider, driver = _paid_ride(22)
        stranger = User.objects.create(email="api-stranger@example.com", keycloak_id="kc-api-stranger")
        self.client.force_authenticate(user=stranger)

        response = self.client.post(reverse("ride_ratings", args=[ride.id]), {"score": 5}, format="json")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_out_of_range_score_is_rejected(self):
        ride, rider, driver = _paid_ride(23)
        self.client.force_authenticate(user=rider)

        response = self.client.post(reverse("ride_ratings", args=[ride.id]), {"score": 6}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_guest_rates_via_tracking_token(self):
        ride, _rider, driver = _paid_ride(24, guest=True)
        response = self.client.post(
            reverse("guest_ride_rating", args=[ride.tracking_token]), {"score": 5}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_driver_rating_summary_is_public(self):
        ride, rider, driver = _paid_ride(25)
        submit_rating(ride, direction=RatingDirection.RIDER_TO_DRIVER, score=5, review="Superb", rater=rider)

        response = self.client.get(reverse("driver_rating_summary", args=[driver.id]))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["average_rating"], 5.0)
        self.assertEqual(response.data["rating_count"], 1)
        self.assertEqual(len(response.data["recent_reviews"]), 1)

    def test_rating_summary_does_not_expose_reviewer_identity(self):
        ride, rider, driver = _paid_ride(26)
        submit_rating(ride, direction=RatingDirection.RIDER_TO_DRIVER, score=5, review="Nice", rater=rider)

        response = self.client.get(reverse("driver_rating_summary", args=[driver.id]))
        serialized = str(response.data)
        self.assertNotIn(rider.email, serialized)

    def test_my_ratings_returns_received_ratings(self):
        ride, rider, driver = _paid_ride(27)
        submit_rating(ride, direction=RatingDirection.RIDER_TO_DRIVER, score=5, rater=rider)

        self.client.force_authenticate(user=driver.user)
        response = self.client.get(reverse("my_ratings"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["rating_count"], 1)
        self.assertEqual(response.data["average_rating"], 5.0)
