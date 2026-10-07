from django.test import TestCase

from apps.drivers.services import apply_as_driver, approve_driver
from apps.identity.services import assign_role
from apps.rides.models import Ride, RideStatus
from apps.rides.services import (
    InvalidRideTransition,
    RidePermissionError,
    accept_ride,
    assign_driver,
    cancel_by_driver,
    cancel_by_rider,
    complete_trip,
    create_ride,
    decline_ride,
    mark_arrived,
    mark_arriving,
    mark_no_driver_found,
    start_trip,
)
from apps.users.models import User
from apps.vehicles.models import VehicleType


def _ride_kwargs(**overrides):
    base = dict(
        vehicle_type=VehicleType.objects.get(name="Sedan"),
        pickup_address="123 Main St",
        pickup_latitude="9.0300",
        pickup_longitude="38.7400",
        destination_address="456 Side St",
        destination_latitude="9.0100",
        destination_longitude="38.7600",
    )
    base.update(overrides)
    return base


class RideCreationTests(TestCase):
    def test_registered_rider_can_create_a_ride(self):
        rider = User.objects.create(email="rider@example.com", keycloak_id="kc-r1")
        ride = create_ride(rider=rider, **_ride_kwargs())

        self.assertEqual(ride.rider, rider)
        self.assertIsNone(ride.guest_phone_number)
        self.assertEqual(ride.status, RideStatus.SEARCHING_DRIVER)
        self.assertFalse(ride.is_guest_ride)

    def test_guest_can_create_a_ride_with_only_a_phone_number(self):
        ride = create_ride(guest_phone_number="+15551234567", guest_name="Sam", **_ride_kwargs())

        self.assertIsNone(ride.rider)
        self.assertEqual(ride.guest_phone_number, "+15551234567")
        self.assertEqual(ride.guest_name, "Sam")
        self.assertEqual(ride.status, RideStatus.SEARCHING_DRIVER)
        self.assertTrue(ride.is_guest_ride)
        self.assertIsNotNone(ride.tracking_token)

    def test_creation_records_full_history_trail(self):
        ride = create_ride(guest_phone_number="+15551234567", **_ride_kwargs())
        history = list(ride.status_history.all())
        self.assertEqual(len(history), 2)
        self.assertEqual((history[0].from_status, history[0].to_status), ("", RideStatus.REQUESTED))
        self.assertEqual((history[1].from_status, history[1].to_status), (RideStatus.REQUESTED, RideStatus.SEARCHING_DRIVER))

    def test_cannot_create_with_both_rider_and_guest_phone(self):
        rider = User.objects.create(email="both@example.com", keycloak_id="kc-both")
        with self.assertRaises(ValueError):
            create_ride(rider=rider, guest_phone_number="+15551234567", **_ride_kwargs())

    def test_cannot_create_with_neither_rider_nor_guest_phone(self):
        with self.assertRaises(ValueError):
            create_ride(**_ride_kwargs())

    def test_db_constraint_rejects_both_set_via_direct_orm_use(self):
        rider = User.objects.create(email="direct@example.com", keycloak_id="kc-direct")
        with self.assertRaises(Exception):
            Ride.objects.create(
                rider=rider, guest_phone_number="+15551234567", **_ride_kwargs()
            )


class RideStateMachineTests(TestCase):
    """Exercises the full lifecycle plus every documented invalid transition."""

    def setUp(self):
        self.rider = User.objects.create(email="lifecycle-rider@example.com", keycloak_id="kc-lc-rider")
        driver_user = User.objects.create(email="lifecycle-driver@example.com", keycloak_id="kc-lc-driver")
        self.admin = User.objects.create(email="lifecycle-admin@example.com", keycloak_id="kc-lc-admin")

        self.driver = apply_as_driver(driver_user, license_number="D1", license_expiry="2030-01-01")
        approve_driver(self.driver, self.admin)

        self.ride = create_ride(rider=self.rider, **_ride_kwargs())

    def test_full_happy_path(self):
        ride = assign_driver(self.ride.id, self.driver, self.admin)
        self.assertEqual(ride.status, RideStatus.DRIVER_ASSIGNED)
        self.assertEqual(ride.driver_id, self.driver.id)

        ride = accept_ride(ride.id, self.driver)
        self.assertEqual(ride.status, RideStatus.DRIVER_ACCEPTED)

        ride = mark_arriving(ride.id, self.driver)
        self.assertEqual(ride.status, RideStatus.DRIVER_ARRIVING)

        ride = mark_arrived(ride.id, self.driver)
        self.assertEqual(ride.status, RideStatus.DRIVER_ARRIVED)

        ride = start_trip(ride.id, self.driver)
        self.assertEqual(ride.status, RideStatus.TRIP_STARTED)
        self.assertIsNotNone(ride.trip_started_at)

        ride = complete_trip(ride.id, self.driver)
        # Auto-advances straight through TRIP_COMPLETED to PAYMENT_PENDING.
        self.assertEqual(ride.status, RideStatus.PAYMENT_PENDING)
        self.assertIsNotNone(ride.trip_completed_at)

        history_statuses = list(ride.status_history.values_list("to_status", flat=True))
        self.assertEqual(
            history_statuses,
            [
                RideStatus.REQUESTED,
                RideStatus.SEARCHING_DRIVER,
                RideStatus.DRIVER_ASSIGNED,
                RideStatus.DRIVER_ACCEPTED,
                RideStatus.DRIVER_ARRIVING,
                RideStatus.DRIVER_ARRIVED,
                RideStatus.TRIP_STARTED,
                RideStatus.TRIP_COMPLETED,
                RideStatus.PAYMENT_PENDING,
            ],
        )

    def test_cannot_assign_an_unapproved_driver(self):
        other_user = User.objects.create(email="unapproved@example.com", keycloak_id="kc-unapp")
        unapproved_driver = apply_as_driver(other_user, license_number="D2", license_expiry="2030-01-01")

        with self.assertRaises(ValueError):
            assign_driver(self.ride.id, unapproved_driver, self.admin)

    def test_cannot_accept_before_being_assigned(self):
        with self.assertRaises(RidePermissionError):
            accept_ride(self.ride.id, self.driver)

    def test_cannot_start_trip_before_arrival(self):
        assign_driver(self.ride.id, self.driver, self.admin)
        accept_ride(self.ride.id, self.driver)
        with self.assertRaises(InvalidRideTransition):
            start_trip(self.ride.id, self.driver)

    def test_cannot_complete_before_starting(self):
        assign_driver(self.ride.id, self.driver, self.admin)
        accept_ride(self.ride.id, self.driver)
        mark_arriving(self.ride.id, self.driver)
        mark_arrived(self.ride.id, self.driver)
        with self.assertRaises(InvalidRideTransition):
            complete_trip(self.ride.id, self.driver)

    def test_decline_returns_ride_to_searching_and_clears_driver(self):
        assign_driver(self.ride.id, self.driver, self.admin)
        ride = decline_ride(self.ride.id, self.driver)

        self.assertEqual(ride.status, RideStatus.SEARCHING_DRIVER)
        self.assertIsNone(ride.driver_id)

    def test_a_different_driver_cannot_act_on_someone_elses_assigned_ride(self):
        other_user = User.objects.create(email="otherdriver2@example.com", keycloak_id="kc-od2")
        other_driver = apply_as_driver(other_user, license_number="D3", license_expiry="2030-01-01")
        approve_driver(other_driver, self.admin)

        assign_driver(self.ride.id, self.driver, self.admin)
        with self.assertRaises(RidePermissionError):
            accept_ride(self.ride.id, other_driver)

    def test_mark_no_driver_found_from_searching(self):
        ride = mark_no_driver_found(self.ride.id, self.admin)
        self.assertEqual(ride.status, RideStatus.NO_DRIVER_FOUND)

    def test_terminal_status_has_no_further_transitions(self):
        ride = mark_no_driver_found(self.ride.id, self.admin)
        self.assertTrue(ride.is_terminal)
        with self.assertRaises(InvalidRideTransition):
            assign_driver(ride.id, self.driver, self.admin)


class RideCancellationTests(TestCase):
    def setUp(self):
        self.rider = User.objects.create(email="cancel-rider@example.com", keycloak_id="kc-cancel-rider")
        driver_user = User.objects.create(email="cancel-driver@example.com", keycloak_id="kc-cancel-driver")
        self.admin = User.objects.create(email="cancel-admin@example.com", keycloak_id="kc-cancel-admin")
        self.driver = apply_as_driver(driver_user, license_number="D5", license_expiry="2030-01-01")
        approve_driver(self.driver, self.admin)

    def test_rider_can_cancel_while_searching(self):
        ride = create_ride(rider=self.rider, **_ride_kwargs())
        ride = cancel_by_rider(ride.id, rider=self.rider, reason="Changed my mind")
        self.assertEqual(ride.status, RideStatus.CANCELLED_BY_RIDER)
        self.assertEqual(ride.cancellation_reason, "Changed my mind")

    def test_guest_can_cancel_with_matching_phone_number(self):
        ride = create_ride(guest_phone_number="+15559998888", **_ride_kwargs())
        ride = cancel_by_rider(ride.id, guest_phone_number="+15559998888")
        self.assertEqual(ride.status, RideStatus.CANCELLED_BY_RIDER)

    def test_guest_cannot_cancel_with_wrong_phone_number(self):
        ride = create_ride(guest_phone_number="+15559998888", **_ride_kwargs())
        with self.assertRaises(RidePermissionError):
            cancel_by_rider(ride.id, guest_phone_number="+15550000000")

    def test_other_rider_cannot_cancel_someone_elses_ride(self):
        ride = create_ride(rider=self.rider, **_ride_kwargs())
        other_rider = User.objects.create(email="notmine@example.com", keycloak_id="kc-notmine")
        with self.assertRaises(RidePermissionError):
            cancel_by_rider(ride.id, rider=other_rider)

    def test_driver_cannot_cancel_before_assignment(self):
        ride = create_ride(rider=self.rider, **_ride_kwargs())
        with self.assertRaises(RidePermissionError):
            cancel_by_driver(ride.id, self.driver)

    def test_driver_can_cancel_after_assignment(self):
        ride = create_ride(rider=self.rider, **_ride_kwargs())
        ride = assign_driver(ride.id, self.driver, self.admin)
        ride = cancel_by_driver(ride.id, self.driver, reason="Vehicle broke down")
        self.assertEqual(ride.status, RideStatus.CANCELLED_BY_DRIVER)

    def test_rider_cannot_cancel_after_trip_started(self):
        ride = create_ride(rider=self.rider, **_ride_kwargs())
        ride = assign_driver(ride.id, self.driver, self.admin)
        accept_ride(ride.id, self.driver)
        mark_arriving(ride.id, self.driver)
        mark_arrived(ride.id, self.driver)
        start_trip(ride.id, self.driver)

        with self.assertRaises(InvalidRideTransition):
            cancel_by_rider(ride.id, rider=self.rider)
