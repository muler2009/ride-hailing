import asyncio

from channels.testing import WebsocketCommunicator
from django.test import TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from apps.dispatch.services import create_offers
from apps.drivers.services import apply_as_driver, approve_driver, set_availability
from apps.identity.services import assign_role
from apps.locations import geo
from apps.locations.consumers import RideTrackingConsumer
from apps.locations.models import DriverLocation
from apps.locations.services import (
    LocationUpdateThrottled,
    MIN_UPDATE_INTERVAL_SECONDS,
    record_location_update,
)
from apps.rides.services import accept_ride, create_ride
from apps.users.models import User
from apps.vehicles.models import VehicleType
from apps.vehicles.services import register_vehicle


def _flush_test_keys():
    client = geo.get_redis_client()
    client.delete(geo.GEO_KEY)
    for pattern in ("locations:driver_seen:*", "locations:throttle:*"):
        for key in client.keys(pattern):
            client.delete(key)


def _ride_kwargs(**overrides):
    base = dict(
        vehicle_type=VehicleType.objects.get(name="Sedan"),
        pickup_address="Pickup",
        pickup_latitude="9.0300",
        pickup_longitude="38.7400",
        destination_address="Destination",
        destination_latitude="9.0100",
        destination_longitude="38.7600",
    )
    base.update(overrides)
    return base


def _make_driver_with_active_ride(email, kc_id, rider):
    """A driver who's online and currently DRIVER_ACCEPTED on a ride — the case record_location_update cares about."""
    user = User.objects.create(email=email, keycloak_id=kc_id)
    assign_role(user, "Driver")
    admin, _ = User.objects.get_or_create(email="locations-admin@example.com", defaults={"keycloak_id": "kc-loc-admin"})
    driver = apply_as_driver(user, license_number=f"D-{kc_id}", license_expiry="2030-01-01")
    approve_driver(driver, admin)
    sedan = VehicleType.objects.get(name="Sedan")
    register_vehicle(driver, vehicle_type=sedan, make="Toyota", model="Corolla", year=2022, license_plate=f"LOC-{kc_id}")
    set_availability(driver, True)

    ride = create_ride(rider=rider, **_ride_kwargs())
    create_offers(ride, [(driver, 0.2)])
    ride = accept_ride(ride.id, driver)
    return driver, ride


class ThrottlingTests(TestCase):
    def setUp(self):
        _flush_test_keys()

    def tearDown(self):
        _flush_test_keys()

    def test_first_update_is_never_throttled(self):
        rider = User.objects.create(email="throttle-rider@example.com", keycloak_id="kc-throttle-rider")
        driver, _ride = _make_driver_with_active_ride("throttle-driver@example.com", "kc-throttle-driver", rider)

        record_location_update(driver, latitude=9.03, longitude=38.74)  # should not raise

    def test_second_immediate_update_is_throttled(self):
        rider = User.objects.create(email="throttle-rider2@example.com", keycloak_id="kc-throttle-rider2")
        driver, _ride = _make_driver_with_active_ride("throttle-driver2@example.com", "kc-throttle-driver2", rider)

        record_location_update(driver, latitude=9.03, longitude=38.74)
        with self.assertRaises(LocationUpdateThrottled):
            record_location_update(driver, latitude=9.031, longitude=38.741)


class SampledPersistenceTests(TestCase):
    def setUp(self):
        _flush_test_keys()

    def tearDown(self):
        _flush_test_keys()

    def test_first_update_on_an_active_ride_persists_a_snapshot(self):
        rider = User.objects.create(email="persist-rider@example.com", keycloak_id="kc-persist-rider")
        driver, ride = _make_driver_with_active_ride("persist-driver@example.com", "kc-persist-driver", rider)

        record_location_update(driver, latitude=9.03, longitude=38.74)

        self.assertEqual(DriverLocation.objects.filter(ride=ride).count(), 1)

    def test_update_with_no_active_ride_does_not_persist(self):
        user = User.objects.create(email="idle-driver@example.com", keycloak_id="kc-idle-driver")
        assign_role(user, "Driver")
        admin = User.objects.create(email="idle-admin@example.com", keycloak_id="kc-idle-admin")
        driver = apply_as_driver(user, license_number="D-IDLE", license_expiry="2030-01-01")
        approve_driver(driver, admin)
        set_availability(driver, True)

        record_location_update(driver, latitude=9.03, longitude=38.74)

        self.assertEqual(DriverLocation.objects.filter(driver=driver).count(), 0)
        # Still updates Redis even with no active ride (useful for a live ops map).
        self.assertIsNotNone(geo.get_driver_location(driver.id))

    def test_rapid_updates_are_sampled_not_all_persisted(self):
        rider = User.objects.create(email="sample-rider@example.com", keycloak_id="kc-sample-rider")
        driver, ride = _make_driver_with_active_ride("sample-driver@example.com", "kc-sample-driver", rider)

        record_location_update(driver, latitude=9.03, longitude=38.74)
        # Manually bypass the per-driver throttle to simulate a second update
        # arriving quickly (throttling and sampling are independent controls).
        geo.get_redis_client().delete(f"locations:throttle:{driver.id}")
        record_location_update(driver, latitude=9.0301, longitude=38.7401)

        # Only one snapshot persisted — the second arrived well within
        # SNAPSHOT_INTERVAL_SECONDS of the first.
        self.assertEqual(DriverLocation.objects.filter(ride=ride).count(), 1)

    def test_old_snapshot_allows_a_new_one(self):
        rider = User.objects.create(email="old-rider@example.com", keycloak_id="kc-old-rider")
        driver, ride = _make_driver_with_active_ride("old-driver@example.com", "kc-old-driver", rider)

        DriverLocation.objects.create(
            driver=driver, ride=ride, latitude=9.03, longitude=38.74,
            recorded_at=timezone.now() - timezone.timedelta(seconds=60),
        )
        record_location_update(driver, latitude=9.031, longitude=38.741)

        self.assertEqual(DriverLocation.objects.filter(ride=ride).count(), 2)


class LocationApiTests(APITestCase):
    def setUp(self):
        _flush_test_keys()
        self.driver_user = User.objects.create(email="locapi-driver@example.com", keycloak_id="kc-locapi-driver")
        assign_role(self.driver_user, "Driver")
        admin = User.objects.create(email="locapi-admin@example.com", keycloak_id="kc-locapi-admin")
        self.driver = apply_as_driver(self.driver_user, license_number="D-LOCAPI", license_expiry="2030-01-01")
        approve_driver(self.driver, admin)
        sedan = VehicleType.objects.get(name="Sedan")
        register_vehicle(self.driver, vehicle_type=sedan, make="Toyota", model="Corolla", year=2022, license_plate="LOCAPI-1")

    def tearDown(self):
        _flush_test_keys()

    def test_second_rapid_request_returns_429(self):
        self.client.force_authenticate(user=self.driver_user)
        self.client.post(reverse("driver_availability"), {"available": True}, format="json")

        first = self.client.post(reverse("driver_location_update"), {"latitude": "9.03", "longitude": "38.74"}, format="json")
        self.assertEqual(first.status_code, status.HTTP_204_NO_CONTENT)

        second = self.client.post(reverse("driver_location_update"), {"latitude": "9.031", "longitude": "38.741"}, format="json")
        self.assertEqual(second.status_code, status.HTTP_429_TOO_MANY_REQUESTS)

    def test_ride_location_history_is_visible_to_the_rider(self):
        rider = User.objects.create(email="history-rider@example.com", keycloak_id="kc-history-rider")
        assign_role(rider, "Rider")
        ride = create_ride(rider=rider, **_ride_kwargs())
        set_availability(self.driver, True)
        create_offers(ride, [(self.driver, 0.3)])
        accept_ride(ride.id, self.driver)

        self.client.force_authenticate(user=self.driver_user)
        self.client.post(reverse("driver_location_update"), {"latitude": "9.03", "longitude": "38.74"}, format="json")

        self.client.force_authenticate(user=rider)
        response = self.client.get(reverse("ride_location_history", args=[ride.id]))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)

    def test_ride_location_history_is_forbidden_to_unrelated_users(self):
        rider = User.objects.create(email="history-rider2@example.com", keycloak_id="kc-history-rider2")
        other_rider = User.objects.create(email="unrelated@example.com", keycloak_id="kc-unrelated")
        ride = create_ride(rider=rider, **_ride_kwargs())

        self.client.force_authenticate(user=other_rider)
        response = self.client.get(reverse("ride_location_history", args=[ride.id]))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class RideTrackingConsumerTests(TransactionTestCase):
    """
    Uses Channels' WebsocketCommunicator against the real Redis-backed
    channel layer (configured in settings.CHANNEL_LAYERS) — not mocked.

    TransactionTestCase (not TestCase) because the status-update broadcast
    path (apps/rides/services.py:_transition) schedules its broadcast via
    transaction.on_commit — those callbacks never fire inside TestCase's
    per-test wrapping transaction, which is rolled back rather than
    committed.

    TransactionTestCase also flushes the whole database after every test,
    which wipes the RBAC/vehicle-type rows seeded by data migrations.
    `serialized_rollback=True` is the usual fix for that, but it collides
    with Django's ContentType framework when more than one
    TransactionTestCase runs in the same suite (a known Django gotcha —
    both this class and apps.dispatch's race-condition test class try to
    reload serialized ContentType rows after their own flush, and the two
    collide on the shared table). Explicitly re-seeding in setUp() AND
    tearDown() instead sidesteps that entirely — tearDown's reseed matters
    for whatever test runs next, of any kind, not just another
    TransactionTestCase.
    """

    def setUp(self):
        from django.core.management import call_command

        call_command("seed_rbac", verbosity=0)
        call_command("seed_vehicle_types", verbosity=0)
        _flush_test_keys()

    def tearDown(self):
        from django.core.management import call_command

        _flush_test_keys()
        call_command("seed_rbac", verbosity=0)
        call_command("seed_vehicle_types", verbosity=0)

    def test_guest_can_connect_with_a_valid_tracking_token(self):
        ride_kwargs = _ride_kwargs()

        async def run():
            from asgiref.sync import sync_to_async

            ride = await sync_to_async(create_ride)(guest_phone_number="+15551112222", **ride_kwargs)
            communicator = WebsocketCommunicator(
                RideTrackingConsumer.as_asgi(), f"/ws/rides/track/{ride.tracking_token}/"
            )
            communicator.scope["url_route"] = {"kwargs": {"tracking_token": str(ride.tracking_token)}}
            connected, _ = await communicator.connect()
            self.assertTrue(connected)
            await communicator.disconnect()

        asyncio.run(run())

    def test_connection_is_rejected_for_an_unknown_tracking_token(self):
        async def run():
            import uuid

            communicator = WebsocketCommunicator(
                RideTrackingConsumer.as_asgi(), f"/ws/rides/track/{uuid.uuid4()}/"
            )
            communicator.scope["url_route"] = {"kwargs": {"tracking_token": str(uuid.uuid4())}}
            connected, subprotocol_or_close_code = await communicator.connect()
            self.assertFalse(connected)

        asyncio.run(run())

    def test_owning_rider_can_connect_by_ride_id(self):
        ride_kwargs = _ride_kwargs()

        async def run():
            from asgiref.sync import sync_to_async
            from django.contrib.auth import get_user_model

            User_ = get_user_model()
            rider = await sync_to_async(User_.objects.create)(email="ws-rider@example.com", keycloak_id="kc-ws-rider")
            ride = await sync_to_async(create_ride)(rider=rider, **ride_kwargs)

            communicator = WebsocketCommunicator(RideTrackingConsumer.as_asgi(), f"/ws/rides/{ride.id}/track/")
            communicator.scope["url_route"] = {"kwargs": {"ride_id": str(ride.id)}}
            communicator.scope["user"] = rider
            connected, _ = await communicator.connect()
            self.assertTrue(connected)
            await communicator.disconnect()

        asyncio.run(run())

    def test_unrelated_authenticated_user_is_rejected(self):
        ride_kwargs = _ride_kwargs()

        async def run():
            from asgiref.sync import sync_to_async
            from django.contrib.auth import get_user_model

            User_ = get_user_model()
            rider = await sync_to_async(User_.objects.create)(email="ws-rider2@example.com", keycloak_id="kc-ws-rider2")
            unrelated = await sync_to_async(User_.objects.create)(email="ws-unrelated@example.com", keycloak_id="kc-ws-unrelated")
            ride = await sync_to_async(create_ride)(rider=rider, **ride_kwargs)

            communicator = WebsocketCommunicator(RideTrackingConsumer.as_asgi(), f"/ws/rides/{ride.id}/track/")
            communicator.scope["url_route"] = {"kwargs": {"ride_id": str(ride.id)}}
            communicator.scope["user"] = unrelated
            connected, _ = await communicator.connect()
            self.assertFalse(connected)

        asyncio.run(run())

    def test_location_update_is_broadcast_to_a_connected_tracker(self):
        async def run():
            from asgiref.sync import sync_to_async
            from django.contrib.auth import get_user_model

            User_ = get_user_model()
            rider = await sync_to_async(User_.objects.create)(email="ws-broadcast-rider@example.com", keycloak_id="kc-ws-broadcast-rider")
            driver, ride = await sync_to_async(_make_driver_with_active_ride)(
                "ws-broadcast-driver@example.com", "kc-ws-broadcast-driver", rider
            )

            communicator = WebsocketCommunicator(RideTrackingConsumer.as_asgi(), f"/ws/rides/{ride.id}/track/")
            communicator.scope["url_route"] = {"kwargs": {"ride_id": str(ride.id)}}
            communicator.scope["user"] = rider
            connected, _ = await communicator.connect()
            self.assertTrue(connected)

            await sync_to_async(record_location_update)(driver, latitude=9.031, longitude=38.741)

            message = await communicator.receive_json_from(timeout=5)
            self.assertEqual(message["event"], "location_update")
            self.assertEqual(message["latitude"], "9.031")

            await communicator.disconnect()

        asyncio.run(run())

    def test_status_transition_is_broadcast_to_a_connected_tracker(self):
        async def run():
            from asgiref.sync import sync_to_async
            from django.contrib.auth import get_user_model

            from apps.rides.services import mark_arriving

            User_ = get_user_model()
            rider = await sync_to_async(User_.objects.create)(email="ws-status-rider@example.com", keycloak_id="kc-ws-status-rider")
            driver, ride = await sync_to_async(_make_driver_with_active_ride)(
                "ws-status-driver@example.com", "kc-ws-status-driver", rider
            )

            communicator = WebsocketCommunicator(RideTrackingConsumer.as_asgi(), f"/ws/rides/{ride.id}/track/")
            communicator.scope["url_route"] = {"kwargs": {"ride_id": str(ride.id)}}
            communicator.scope["user"] = rider
            connected, _ = await communicator.connect()
            self.assertTrue(connected)

            await sync_to_async(mark_arriving)(ride.id, driver)

            message = await communicator.receive_json_from(timeout=5)
            self.assertEqual(message["event"], "status_update")
            self.assertEqual(message["status"], "DRIVER_ARRIVING")

            await communicator.disconnect()

        asyncio.run(run())
