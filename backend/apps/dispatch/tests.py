import threading
import uuid

from django.test import TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from apps.locations import geo
from apps.dispatch.models import DispatchOffer, DispatchOfferStatus
from apps.dispatch.services import (
    create_offers,
    dispatch_ride,
    expire_stale_offers,
    find_candidate_drivers,
    run_dispatch_cycle,
)
from apps.drivers.services import apply_as_driver, approve_driver, set_availability
from apps.identity.services import assign_role
from apps.rides.models import RideStatus
from apps.rides.services import accept_ride, create_ride, decline_ride
from apps.users.models import User
from apps.vehicles.models import VehicleType
from apps.vehicles.services import register_vehicle


def _flush_test_geo_keys():
    """Redis state isn't part of Django's per-test transaction rollback, so clean it explicitly."""
    client = geo.get_redis_client()
    client.delete(geo.GEO_KEY)
    for key in client.keys("locations:driver_seen:*"):
        client.delete(key)


ADDIS_ABABA_PICKUP = (9.0300, 38.7400)
NEARBY_POINT = (9.0320, 38.7420)  # ~0.3km away
FAR_POINT = (9.4000, 38.9000)  # well outside a 5km search radius


def _ride_kwargs(**overrides):
    base = dict(
        vehicle_type=VehicleType.objects.get(name="Sedan"),
        pickup_address="Pickup",
        pickup_latitude=str(ADDIS_ABABA_PICKUP[0]),
        pickup_longitude=str(ADDIS_ABABA_PICKUP[1]),
        destination_address="Destination",
        destination_latitude="9.0100",
        destination_longitude="38.7600",
    )
    base.update(overrides)
    return base


def _make_approved_driver(email, kc_id, *, available=True, location=NEARBY_POINT, vehicle_type_name="Sedan"):
    user = User.objects.create(email=email, keycloak_id=kc_id)
    assign_role(user, "Driver")
    admin = User.objects.filter(email="dispatch-admin@example.com").first()
    if admin is None:
        admin = User.objects.create(email="dispatch-admin@example.com", keycloak_id="kc-dispatch-admin")
    driver = apply_as_driver(user, license_number=f"D-{kc_id}", license_expiry="2030-01-01")
    approve_driver(driver, admin)
    vehicle_type = VehicleType.objects.get(name=vehicle_type_name)
    register_vehicle(driver, vehicle_type=vehicle_type, make="Toyota", model="Corolla", year=2022, license_plate=f"PLT-{kc_id}")
    if available:
        set_availability(driver, True)
        geo.set_driver_location(driver.id, latitude=location[0], longitude=location[1])
    return driver


class GeoStoreTests(TestCase):
    def setUp(self):
        _flush_test_geo_keys()

    def tearDown(self):
        _flush_test_geo_keys()

    def test_set_and_get_driver_location(self):
        driver_id = uuid.uuid4()
        geo.set_driver_location(driver_id, latitude=9.03, longitude=38.74)
        location = geo.get_driver_location(driver_id)
        self.assertIsNotNone(location)
        lat, lng = location
        self.assertAlmostEqual(lat, 9.03, places=3)
        self.assertAlmostEqual(lng, 38.74, places=3)

    def test_location_is_fresh_immediately_after_setting(self):
        driver_id = uuid.uuid4()
        geo.set_driver_location(driver_id, latitude=9.03, longitude=38.74)
        self.assertTrue(geo.is_location_fresh(driver_id))

    def test_remove_driver_location_clears_both_keys(self):
        driver_id = uuid.uuid4()
        geo.set_driver_location(driver_id, latitude=9.03, longitude=38.74)
        geo.remove_driver_location(driver_id)
        self.assertIsNone(geo.get_driver_location(driver_id))
        self.assertFalse(geo.is_location_fresh(driver_id))

    def test_find_nearby_returns_sorted_by_distance(self):
        near_id, far_id = uuid.uuid4(), uuid.uuid4()
        geo.set_driver_location(near_id, latitude=NEARBY_POINT[0], longitude=NEARBY_POINT[1])
        geo.set_driver_location(far_id, latitude=FAR_POINT[0], longitude=FAR_POINT[1])

        results = geo.find_nearby_driver_ids(
            latitude=ADDIS_ABABA_PICKUP[0], longitude=ADDIS_ABABA_PICKUP[1], radius_km=5, count=10
        )
        result_ids = [r[0] for r in results]
        self.assertIn(str(near_id), result_ids)
        self.assertNotIn(str(far_id), result_ids)


class FindCandidateDriversTests(TestCase):
    def setUp(self):
        _flush_test_geo_keys()
        self.ride = create_ride(
            rider=User.objects.create(email="candidate-rider@example.com", keycloak_id="kc-cand-rider"),
            **_ride_kwargs(),
        )

    def tearDown(self):
        _flush_test_geo_keys()

    def test_finds_an_eligible_nearby_driver(self):
        driver = _make_approved_driver("cand1@example.com", "kc-cand1")
        candidates = find_candidate_drivers(self.ride)
        self.assertEqual([d.id for d, _ in candidates], [driver.id])

    def test_excludes_offline_drivers(self):
        _make_approved_driver("cand2@example.com", "kc-cand2", available=False)
        candidates = find_candidate_drivers(self.ride)
        self.assertEqual(candidates, [])

    def test_excludes_drivers_outside_radius(self):
        _make_approved_driver("cand3@example.com", "kc-cand3", location=FAR_POINT)
        candidates = find_candidate_drivers(self.ride)
        self.assertEqual(candidates, [])

    def test_excludes_drivers_with_wrong_vehicle_type(self):
        _make_approved_driver("cand4@example.com", "kc-cand4", vehicle_type_name="Van")
        candidates = find_candidate_drivers(self.ride)  # ride requests a Sedan
        self.assertEqual(candidates, [])

    def test_excludes_drivers_already_on_an_active_ride(self):
        driver = _make_approved_driver("cand5@example.com", "kc-cand5")
        other_ride = create_ride(
            rider=User.objects.create(email="other-rider@example.com", keycloak_id="kc-other-rider"),
            **_ride_kwargs(),
        )
        other_ride.driver = driver
        other_ride.status = RideStatus.DRIVER_ACCEPTED
        other_ride.save()

        candidates = find_candidate_drivers(self.ride)
        self.assertEqual(candidates, [])

    def test_excludes_drivers_already_offered_this_ride(self):
        driver = _make_approved_driver("cand6@example.com", "kc-cand6")
        create_offers(self.ride, [(driver, 0.3)])
        candidates = find_candidate_drivers(self.ride)
        self.assertEqual(candidates, [])

    def test_nearer_driver_ranks_first(self):
        _make_approved_driver("cand7@example.com", "kc-cand7", location=(9.0500, 38.7700))
        near = _make_approved_driver("cand8@example.com", "kc-cand8", location=NEARBY_POINT)

        candidates = find_candidate_drivers(self.ride)
        self.assertEqual(candidates[0][0].id, near.id)


class DispatchOfferAndAcceptRaceTests(TransactionTestCase):
    """
    TransactionTestCase (not TestCase) so real threads can hold separate DB
    connections and genuinely race — TestCase's transaction-wrapped tests
    would serialize on Django's own test-transaction machinery rather than
    exercising select_for_update the way concurrent requests actually would.

    TransactionTestCase flushes the whole database after every test, which
    wipes the RBAC/vehicle-type rows seeded by data migrations — including
    for whichever test runs next, in this class or another. Explicitly
    re-seeding in setUp() (rather than relying on Django's
    serialized_rollback, which collides with the ContentType framework
    when more than one TransactionTestCase is in the suite) keeps this
    self-contained regardless of test execution order.
    """

    def setUp(self):
        from django.core.management import call_command

        call_command("seed_rbac", verbosity=0)
        call_command("seed_vehicle_types", verbosity=0)
        _flush_test_geo_keys()

    def tearDown(self):
        from django.core.management import call_command

        _flush_test_geo_keys()
        # Restore seed data immediately: TransactionTestCase's own flush
        # just wiped it, and whatever test runs next (of any kind, not
        # just another TransactionTestCase) may assume it's there.
        call_command("seed_rbac", verbosity=0)
        call_command("seed_vehicle_types", verbosity=0)

    def test_only_one_of_two_offered_drivers_can_win_the_ride(self):
        rider = User.objects.create(email="race-rider@example.com", keycloak_id="kc-race-rider")
        ride = create_ride(rider=rider, **_ride_kwargs())

        d1 = _make_approved_driver("race-d1@example.com", "kc-race-d1")
        d2 = _make_approved_driver("race-d2@example.com", "kc-race-d2")
        create_offers(ride, [(d1, 0.2), (d2, 0.4)])

        outcomes = {}

        def try_accept(driver, key):
            try:
                accept_ride(ride.id, driver)
                outcomes[key] = "won"
            except Exception as exc:
                outcomes[key] = f"lost: {exc}"

        t1 = threading.Thread(target=try_accept, args=(d1, "d1"))
        t2 = threading.Thread(target=try_accept, args=(d2, "d2"))
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        winners = [k for k, v in outcomes.items() if v == "won"]
        self.assertEqual(len(winners), 1, f"expected exactly one winner, got: {outcomes}")

        ride.refresh_from_db()
        self.assertEqual(ride.status, RideStatus.DRIVER_ACCEPTED)
        winning_driver_id = d1.id if winners[0] == "d1" else d2.id
        self.assertEqual(ride.driver_id, winning_driver_id)

        offers = {o.driver_id: o.status for o in DispatchOffer.objects.filter(ride=ride)}
        self.assertEqual(offers[winning_driver_id], DispatchOfferStatus.ACCEPTED)
        losing_driver_id = d2.id if winners[0] == "d1" else d1.id
        self.assertEqual(offers[losing_driver_id], DispatchOfferStatus.SUPERSEDED)


class OfferAcceptDeclineServiceTests(TestCase):
    def setUp(self):
        _flush_test_geo_keys()
        self.rider = User.objects.create(email="offer-rider@example.com", keycloak_id="kc-offer-rider")
        self.ride = create_ride(rider=self.rider, **_ride_kwargs())
        self.driver = _make_approved_driver("offer-driver@example.com", "kc-offer-driver")
        create_offers(self.ride, [(self.driver, 0.3)])

    def tearDown(self):
        _flush_test_geo_keys()

    def test_accepting_a_dispatch_offer_assigns_and_accepts_in_one_step(self):
        ride = accept_ride(self.ride.id, self.driver)
        self.assertEqual(ride.status, RideStatus.DRIVER_ACCEPTED)
        self.assertEqual(ride.driver_id, self.driver.id)

        offer = DispatchOffer.objects.get(ride=ride, driver=self.driver)
        self.assertEqual(offer.status, DispatchOfferStatus.ACCEPTED)

        self.driver.refresh_from_db()
        self.assertEqual(self.driver.total_offers_accepted, 1)

    def test_declining_a_dispatch_offer_leaves_ride_searching(self):
        ride = decline_ride(self.ride.id, self.driver)
        self.assertEqual(ride.status, RideStatus.SEARCHING_DRIVER)
        self.assertIsNone(ride.driver_id)

        offer = DispatchOffer.objects.get(ride=ride, driver=self.driver)
        self.assertEqual(offer.status, DispatchOfferStatus.DECLINED)

    def test_driver_without_an_offer_cannot_accept(self):
        other_driver = _make_approved_driver("no-offer@example.com", "kc-no-offer")
        from apps.rides.services import RidePermissionError

        with self.assertRaises(RidePermissionError):
            accept_ride(self.ride.id, other_driver)

    def test_expired_offer_cannot_be_accepted(self):
        offer = DispatchOffer.objects.get(ride=self.ride, driver=self.driver)
        offer.expires_at = timezone.now() - timezone.timedelta(seconds=1)
        offer.save(update_fields=["expires_at"])

        from apps.rides.services import RidePermissionError

        with self.assertRaises(RidePermissionError):
            accept_ride(self.ride.id, self.driver)


class DispatchRideAndCycleTests(TestCase):
    def setUp(self):
        _flush_test_geo_keys()

    def tearDown(self):
        _flush_test_geo_keys()

    def test_dispatch_ride_creates_offers_for_eligible_candidates(self):
        rider = User.objects.create(email="dr-rider@example.com", keycloak_id="kc-dr-rider")
        ride = create_ride(rider=rider, **_ride_kwargs())
        driver = _make_approved_driver("dr-driver@example.com", "kc-dr-driver")

        offers = dispatch_ride(ride)
        self.assertEqual(len(offers), 1)
        self.assertEqual(offers[0].driver_id, driver.id)

        driver.refresh_from_db()
        self.assertEqual(driver.total_offers_sent, 1)

    def test_dispatch_ride_with_no_candidates_creates_no_offers(self):
        rider = User.objects.create(email="dr-rider2@example.com", keycloak_id="kc-dr-rider2")
        ride = create_ride(rider=rider, **_ride_kwargs())
        self.assertEqual(dispatch_ride(ride), [])

    def test_expire_stale_offers(self):
        rider = User.objects.create(email="dr-rider3@example.com", keycloak_id="kc-dr-rider3")
        ride = create_ride(rider=rider, **_ride_kwargs())
        driver = _make_approved_driver("dr-driver3@example.com", "kc-dr-driver3")
        offer = create_offers(ride, [(driver, 0.5)])[0]
        offer.expires_at = timezone.now() - timezone.timedelta(seconds=1)
        offer.save(update_fields=["expires_at"])

        count = expire_stale_offers()
        self.assertEqual(count, 1)
        offer.refresh_from_db()
        self.assertEqual(offer.status, DispatchOfferStatus.EXPIRED)

    def test_run_dispatch_cycle_dispatches_a_searching_ride(self):
        rider = User.objects.create(email="cycle-rider@example.com", keycloak_id="kc-cycle-rider")
        ride = create_ride(rider=rider, **_ride_kwargs())
        # No dispatch happened yet in this test (create_ride itself doesn't
        # call dispatch — that's the view layer's job), so the cycle should
        # find this ride and dispatch it.
        driver = _make_approved_driver("cycle-driver@example.com", "kc-cycle-driver")

        results = run_dispatch_cycle()
        self.assertEqual(results["dispatched"], 1)
        self.assertTrue(DispatchOffer.objects.filter(ride=ride, driver=driver).exists())

    def test_run_dispatch_cycle_marks_no_driver_found_after_search_window(self):
        rider = User.objects.create(email="cycle-rider2@example.com", keycloak_id="kc-cycle-rider2")
        ride = create_ride(rider=rider, **_ride_kwargs())
        from apps.dispatch.services import MAX_SEARCH_SECONDS

        ride.created_at = timezone.now() - timezone.timedelta(seconds=MAX_SEARCH_SECONDS + 10)
        ride.save(update_fields=["created_at"])

        results = run_dispatch_cycle()
        self.assertEqual(results["no_driver_found"], 1)
        ride.refresh_from_db()
        self.assertEqual(ride.status, RideStatus.NO_DRIVER_FOUND)

    def test_admin_manual_assign_supersedes_pending_offers(self):
        rider = User.objects.create(email="override-rider@example.com", keycloak_id="kc-override-rider")
        admin = User.objects.create(email="override-admin@example.com", keycloak_id="kc-override-admin")
        ride = create_ride(rider=rider, **_ride_kwargs())

        offered_driver = _make_approved_driver("override-offered@example.com", "kc-override-offered")
        create_offers(ride, [(offered_driver, 0.3)])

        manually_assigned = _make_approved_driver(
            "override-manual@example.com", "kc-override-manual", location=FAR_POINT
        )
        from apps.rides.services import assign_driver

        assign_driver(ride.id, manually_assigned, admin)

        offer = DispatchOffer.objects.get(ride=ride, driver=offered_driver)
        self.assertEqual(offer.status, DispatchOfferStatus.SUPERSEDED)


class DispatchApiTests(APITestCase):
    def setUp(self):
        _flush_test_geo_keys()
        self.driver_user = User.objects.create(email="api-driver@example.com", keycloak_id="kc-api-driver")
        assign_role(self.driver_user, "Driver")
        admin = User.objects.create(email="api-admin@example.com", keycloak_id="kc-api-admin")
        self.driver = apply_as_driver(self.driver_user, license_number="D-API", license_expiry="2030-01-01")
        approve_driver(self.driver, admin)
        sedan = VehicleType.objects.get(name="Sedan")
        register_vehicle(self.driver, vehicle_type=sedan, make="Toyota", model="Corolla", year=2022, license_plate="API-1")

    def tearDown(self):
        _flush_test_geo_keys()

    def test_driver_must_be_online_to_update_location(self):
        self.client.force_authenticate(user=self.driver_user)
        response = self.client.post(
            reverse("driver_location_update"), {"latitude": "9.03", "longitude": "38.74"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_driver_can_update_location_once_online(self):
        self.client.force_authenticate(user=self.driver_user)
        self.client.post(reverse("driver_availability"), {"available": True}, format="json")

        response = self.client.post(
            reverse("driver_location_update"), {"latitude": "9.03", "longitude": "38.74"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertIsNotNone(geo.get_driver_location(self.driver.id))

    def test_going_offline_clears_location(self):
        self.client.force_authenticate(user=self.driver_user)
        self.client.post(reverse("driver_availability"), {"available": True}, format="json")
        self.client.post(reverse("driver_location_update"), {"latitude": "9.03", "longitude": "38.74"}, format="json")

        self.client.post(reverse("driver_availability"), {"available": False}, format="json")
        self.assertIsNone(geo.get_driver_location(self.driver.id))

    def test_unapproved_driver_cannot_go_online(self):
        user2 = User.objects.create(email="unapproved-online@example.com", keycloak_id="kc-unapp-online")
        assign_role(user2, "Driver")
        apply_as_driver(user2, license_number="D-UNAPP", license_expiry="2030-01-01")

        self.client.force_authenticate(user=user2)
        response = self.client.post(reverse("driver_availability"), {"available": True}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_driver_sees_pending_offers(self):
        rider = User.objects.create(email="offers-rider@example.com", keycloak_id="kc-offers-rider")
        ride = create_ride(rider=rider, **_ride_kwargs())
        create_offers(ride, [(self.driver, 0.4)])

        self.client.force_authenticate(user=self.driver_user)
        response = self.client.get(reverse("my_pending_offers"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["results"]), 1)
        self.assertEqual(response.data["results"][0]["ride_id"], str(ride.id))

    def test_full_guest_ride_gets_auto_dispatched_end_to_end(self):
        """
        The end-to-end path this feature exists for: a guest requests a
        ride with just a phone number, gets automatically offered to a
        nearby online driver, and the driver accepts it — no admin
        involved anywhere in the flow.
        """
        self.client.force_authenticate(user=self.driver_user)
        self.client.post(reverse("driver_availability"), {"available": True}, format="json")
        self.client.post(
            reverse("driver_location_update"),
            {"latitude": str(NEARBY_POINT[0]), "longitude": str(NEARBY_POINT[1])},
            format="json",
        )
        self.client.force_authenticate(user=None)

        sedan = VehicleType.objects.get(name="Sedan")
        create_response = self.client.post(
            reverse("ride_create"),
            {
                "vehicle_type_id": str(sedan.id),
                "pickup_address": "Somewhere",
                "pickup_latitude": str(ADDIS_ABABA_PICKUP[0]),
                "pickup_longitude": str(ADDIS_ABABA_PICKUP[1]),
                "destination_address": "Elsewhere",
                "destination_latitude": "9.0100",
                "destination_longitude": "38.7600",
                "guest_phone_number": "+15557654321",
            },
            format="json",
        )
        self.assertEqual(create_response.status_code, status.HTTP_201_CREATED)
        ride_id = create_response.data["id"]

        self.client.force_authenticate(user=self.driver_user)
        offers_response = self.client.get(reverse("my_pending_offers"))
        self.assertEqual(len(offers_response.data["results"]), 1)
        self.assertEqual(offers_response.data["results"][0]["ride_id"], ride_id)

        accept_response = self.client.post(reverse("ride_accept", args=[ride_id]))
        self.assertEqual(accept_response.status_code, status.HTTP_200_OK)
        self.assertEqual(accept_response.data["status"], "DRIVER_ACCEPTED")
