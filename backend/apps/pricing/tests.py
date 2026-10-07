from decimal import Decimal

from django.test import TestCase
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from apps.dispatch.services import create_offers
from apps.drivers.services import apply_as_driver, approve_driver
from apps.identity.services import assign_role
from apps.pricing.models import Fare, FareLineItemType, FareType, PricingRule
from apps.pricing.services import (
    PricingError,
    apply_cancellation_fee,
    calculate_fare,
    create_fare_record,
    estimate_fare_for_ride,
    finalize_fare_for_ride,
    get_pricing_rule,
)
from apps.rides.services import (
    accept_ride,
    cancel_by_rider,
    complete_trip,
    create_ride,
    mark_arrived,
    mark_arriving,
    start_trip,
)
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


class CalculateFareTests(TestCase):
    """
    The core "no float drift" guarantee: identical inputs always produce
    identical, exact output, and every line item sums to the reported
    total with no rounding remainder.
    """

    def setUp(self):
        self.sedan = VehicleType.objects.get(name="Sedan")
        self.rule = get_pricing_rule(self.sedan)

    def test_basic_fare_matches_hand_calculation(self):
        # Sedan: base=2.00, per_km=0.50, per_minute=0.10, minimum=3.00
        breakdown = calculate_fare(
            pricing_rule=self.rule, distance_km=Decimal("5"), duration_minutes=Decimal("10")
        )
        # 2.00 + (0.50*5=2.50) + (0.10*10=1.00) = 5.50, above the 3.00 minimum
        self.assertEqual(breakdown.total_amount, Decimal("5.50"))

        by_type = {item.item_type: item.amount for item in breakdown.line_items}
        self.assertEqual(by_type[FareLineItemType.BASE_FARE], Decimal("2.00"))
        self.assertEqual(by_type[FareLineItemType.DISTANCE_FARE], Decimal("2.50"))
        self.assertEqual(by_type[FareLineItemType.DURATION_FARE], Decimal("1.00"))

    def test_line_items_sum_exactly_to_total_with_no_drift(self):
        breakdown = calculate_fare(
            pricing_rule=self.rule, distance_km=Decimal("7.33"), duration_minutes=Decimal("13.7")
        )
        self.assertEqual(sum(item.amount for item in breakdown.line_items), breakdown.total_amount)

    def test_repeated_calculation_is_perfectly_deterministic(self):
        results = [
            calculate_fare(pricing_rule=self.rule, distance_km=Decimal("4.2"), duration_minutes=Decimal("9.8")).total_amount
            for _ in range(20)
        ]
        self.assertEqual(len(set(results)), 1)

    def test_short_trip_is_bumped_to_minimum_fare(self):
        breakdown = calculate_fare(
            pricing_rule=self.rule, distance_km=Decimal("0.5"), duration_minutes=Decimal("1")
        )
        # 2.00 + 0.25 + 0.10 = 2.35, below the 3.00 minimum
        self.assertEqual(breakdown.total_amount, Decimal("3.00"))
        by_type = {item.item_type: item.amount for item in breakdown.line_items}
        self.assertEqual(by_type[FareLineItemType.MINIMUM_FARE_ADJUSTMENT], Decimal("0.65"))

    def test_surge_multiplies_only_the_metered_subtotal(self):
        self.rule.surge_multiplier = Decimal("1.50")
        breakdown = calculate_fare(
            pricing_rule=self.rule,
            distance_km=Decimal("5"), duration_minutes=Decimal("10"),
            waiting_minutes=Decimal("2"), tolls=Decimal("1.00"),
        )
        metered = Decimal("2.00") + Decimal("2.50") + Decimal("1.00")  # 5.50
        expected_surge = (metered * Decimal("0.5"))  # 2.75
        by_type = {item.item_type: item.amount for item in breakdown.line_items}
        self.assertEqual(by_type[FareLineItemType.SURGE_ADJUSTMENT], expected_surge)
        # waiting (0.10*2=0.20) and tolls (1.00) are NOT surged
        self.assertEqual(by_type[FareLineItemType.WAITING_FARE], Decimal("0.20"))
        self.assertEqual(by_type[FareLineItemType.TOLLS], Decimal("1.00"))
        self.assertEqual(breakdown.total_amount, metered + expected_surge + Decimal("0.20") + Decimal("1.00"))

    def test_tax_applied_after_minimum_fare_adjustment(self):
        self.rule.tax_rate = Decimal("0.10")
        breakdown = calculate_fare(
            pricing_rule=self.rule, distance_km=Decimal("0.5"), duration_minutes=Decimal("1")
        )
        # subtotal bumped to 3.00 by minimum fare, then 10% tax = 0.30
        by_type = {item.item_type: item.amount for item in breakdown.line_items}
        self.assertEqual(by_type[FareLineItemType.TAX], Decimal("0.30"))
        self.assertEqual(breakdown.total_amount, Decimal("3.30"))

    def test_discount_reduces_total_but_never_below_zero(self):
        breakdown = calculate_fare(
            pricing_rule=self.rule,
            distance_km=Decimal("5"), duration_minutes=Decimal("10"),
            discount_amount=Decimal("100.00"),
        )
        self.assertEqual(breakdown.total_amount, Decimal("0.00"))
        by_type = {item.item_type: item.amount for item in breakdown.line_items}
        self.assertEqual(by_type[FareLineItemType.DISCOUNT], Decimal("-5.50"))

    def test_zero_valued_optional_line_items_are_omitted(self):
        breakdown = calculate_fare(
            pricing_rule=self.rule, distance_km=Decimal("5"), duration_minutes=Decimal("10")
        )
        item_types = {item.item_type for item in breakdown.line_items}
        self.assertNotIn(FareLineItemType.SURGE_ADJUSTMENT, item_types)
        self.assertNotIn(FareLineItemType.WAITING_FARE, item_types)
        self.assertNotIn(FareLineItemType.TOLLS, item_types)
        self.assertNotIn(FareLineItemType.TAX, item_types)
        self.assertNotIn(FareLineItemType.DISCOUNT, item_types)

    def test_missing_pricing_rule_raises(self):
        moto = VehicleType.objects.get(name="Moto")
        PricingRule.objects.filter(vehicle_type=moto).delete()
        # Re-fetch: `moto.pricing_rule` would otherwise return Django's
        # cached reverse-OneToOne descriptor value from before the delete.
        fresh_moto = VehicleType.objects.get(pk=moto.pk)
        with self.assertRaises(PricingError):
            get_pricing_rule(fresh_moto)


class FarePersistenceTests(TestCase):
    def setUp(self):
        self.rider = User.objects.create(email="fare-rider@example.com", keycloak_id="kc-fare-rider")
        self.ride = create_ride(rider=self.rider, **_ride_kwargs())
        self.ride.estimated_distance_km = Decimal("5.0")
        self.ride.estimated_duration_minutes = Decimal("10.0")
        self.ride.save()

    def test_estimate_fare_creates_fare_and_line_items(self):
        fare = estimate_fare_for_ride(self.ride)
        self.assertEqual(fare.fare_type, FareType.ESTIMATE)
        self.assertGreater(fare.line_items.count(), 0)
        self.assertEqual(sum(i.amount for i in fare.line_items.all()), fare.total_amount)

    def test_estimate_can_be_recalculated(self):
        estimate_fare_for_ride(self.ride)
        self.ride.estimated_distance_km = Decimal("20.0")
        self.ride.save()
        second = estimate_fare_for_ride(self.ride)

        self.assertEqual(Fare.objects.filter(ride=self.ride, fare_type=FareType.ESTIMATE).count(), 1)
        self.assertEqual(second.distance_km, Decimal("20.0"))

    def test_final_fare_cannot_be_recalculated(self):
        finalize_fare_for_ride(self.ride)
        with self.assertRaises(PricingError):
            finalize_fare_for_ride(self.ride)

    def test_finalize_falls_back_to_ride_estimate_when_no_actuals_given(self):
        fare = finalize_fare_for_ride(self.ride)
        self.assertEqual(fare.distance_km, Decimal("5.0"))
        self.assertEqual(fare.duration_minutes, Decimal("10.0"))

    def test_finalize_uses_explicit_actuals_when_given(self):
        fare = finalize_fare_for_ride(self.ride, distance_km=Decimal("6.5"), duration_minutes=Decimal("14"))
        self.assertEqual(fare.distance_km, Decimal("6.5"))
        self.assertEqual(fare.duration_minutes, Decimal("14"))

    def test_float_route_estimate_does_not_corrupt_the_fare(self):
        """
        Guards the exact bug this phase's _as_decimal() boundary exists to
        prevent: a Ride field holding a raw float in memory (as it would
        right after `ride.estimated_distance_km = route.distance_km` from
        apps.maps, before any DB round-trip) must still produce an exact
        Decimal fare, not one contaminated by float representation error.
        """
        self.ride.estimated_distance_km = 5.0  # raw float, deliberately not Decimal
        self.ride.estimated_duration_minutes = 10.0
        fare = estimate_fare_for_ride(self.ride)
        self.assertEqual(fare.total_amount, Decimal("5.50"))

    def test_estimate_without_a_route_estimate_raises(self):
        rider2 = User.objects.create(email="fare-rider2@example.com", keycloak_id="kc-fare-rider2")
        bare_ride = create_ride(rider=rider2, **_ride_kwargs())
        with self.assertRaises(PricingError):
            estimate_fare_for_ride(bare_ride)


class RideLifecyclePricingIntegrationTests(TestCase):
    """Confirms the pricing engine is actually wired into ride creation, completion, and cancellation."""

    def setUp(self):
        self.rider = User.objects.create(email="lifecycle-fare-rider@example.com", keycloak_id="kc-lf-rider")
        driver_user = User.objects.create(email="lifecycle-fare-driver@example.com", keycloak_id="kc-lf-driver")
        admin = User.objects.create(email="lifecycle-fare-admin@example.com", keycloak_id="kc-lf-admin")
        self.driver = apply_as_driver(driver_user, license_number="D1", license_expiry="2030-01-01")
        approve_driver(self.driver, admin)
        sedan = VehicleType.objects.get(name="Sedan")
        register_vehicle(self.driver, vehicle_type=sedan, make="Toyota", model="Corolla", year=2022, license_plate="FARE-1")

        self.ride = create_ride(rider=self.rider, **_ride_kwargs())
        self.ride.estimated_distance_km = Decimal("5.0")
        self.ride.estimated_duration_minutes = Decimal("10.0")
        self.ride.save()
        create_offers(self.ride, [(self.driver, 0.3)])

    def test_completing_a_trip_creates_a_final_fare(self):
        accept_ride(self.ride.id, self.driver)
        mark_arriving(self.ride.id, self.driver)
        mark_arrived(self.ride.id, self.driver)
        start_trip(self.ride.id, self.driver)
        complete_trip(self.ride.id, self.driver)

        self.assertTrue(Fare.objects.filter(ride=self.ride, fare_type=FareType.FINAL).exists())

    def test_cancelling_after_acceptance_charges_a_fee(self):
        accept_ride(self.ride.id, self.driver)
        cancel_by_rider(self.ride.id, rider=self.rider, reason="changed my mind")

        fee = Fare.objects.get(ride=self.ride, fare_type=FareType.CANCELLATION)
        self.assertEqual(fee.total_amount, self.driver.vehicles.first().vehicle_type.pricing_rule.cancellation_fee)

    def test_cancelling_before_acceptance_charges_no_fee(self):
        # Ride is still SEARCHING_DRIVER — no fee should apply.
        cancel_by_rider(self.ride.id, rider=self.rider, reason="changed my mind")
        self.assertFalse(Fare.objects.filter(ride=self.ride, fare_type=FareType.CANCELLATION).exists())


class PricingApiTests(APITestCase):
    def setUp(self):
        self.sedan = VehicleType.objects.get(name="Sedan")
        self.ops_admin = User.objects.create(email="pricing-admin@example.com", keycloak_id="kc-pricing-admin")
        assign_role(self.ops_admin, "Operations Manager")
        self.rider = User.objects.create(email="pricing-rider@example.com", keycloak_id="kc-pricing-rider")
        assign_role(self.rider, "Rider")

    def test_pricing_rule_list_is_public(self):
        response = self.client.get(reverse("pricing_rule_list"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 4)

    def test_pricing_rule_detail_is_public(self):
        response = self.client.get(reverse("pricing_rule_detail", args=[self.sedan.id]))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["vehicle_type"]["name"], "Sedan")

    def test_rider_cannot_update_pricing_rule(self):
        self.client.force_authenticate(user=self.rider)
        response = self.client.put(
            reverse("pricing_rule_detail", args=[self.sedan.id]),
            {"base_fare": "9.99", "per_km_rate": "1.00", "per_minute_rate": "0.20", "minimum_fare": "5.00"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_ops_manager_can_update_pricing_rule(self):
        self.client.force_authenticate(user=self.ops_admin)
        response = self.client.put(
            reverse("pricing_rule_detail", args=[self.sedan.id]),
            {"base_fare": "9.99", "per_km_rate": "1.00", "per_minute_rate": "0.20", "minimum_fare": "5.00"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["base_fare"], "9.99")

        self.sedan.pricing_rule.refresh_from_db()
        self.assertEqual(self.sedan.pricing_rule.base_fare, Decimal("9.99"))

    def test_ride_fares_endpoint_visible_to_own_rider(self):
        ride = create_ride(rider=self.rider, **_ride_kwargs())
        ride.estimated_distance_km = Decimal("5.0")
        ride.estimated_duration_minutes = Decimal("10.0")
        ride.save()
        estimate_fare_for_ride(ride)

        self.client.force_authenticate(user=self.rider)
        response = self.client.get(reverse("ride_fare_list", args=[ride.id]))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["fare_type"], "ESTIMATE")

    def test_ride_fares_endpoint_forbidden_to_unrelated_user(self):
        ride = create_ride(rider=self.rider, **_ride_kwargs())
        other = User.objects.create(email="unrelated-fare@example.com", keycloak_id="kc-unrelated-fare")

        self.client.force_authenticate(user=other)
        response = self.client.get(reverse("ride_fare_list", args=[ride.id]))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class RideCreationFareIntegrationTests(APITestCase):
    """Confirms ride creation itself produces a fare estimate end-to-end over the API."""

    def test_creating_a_ride_with_a_mocked_route_produces_a_fare_estimate(self):
        from unittest import mock

        from apps.maps.base import RouteResult

        sedan = VehicleType.objects.get(name="Sedan")
        with mock.patch("apps.rides.views.get_map_provider") as mock_get_provider:
            mock_provider = mock.Mock()
            mock_provider.get_route.return_value = RouteResult(distance_km=5.0, duration_minutes=10.0)
            mock_get_provider.return_value = mock_provider

            response = self.client.post(
                reverse("ride_create"),
                {
                    "vehicle_type_id": str(sedan.id),
                    "pickup_address": "A", "pickup_latitude": "9.03", "pickup_longitude": "38.74",
                    "destination_address": "B", "destination_latitude": "9.01", "destination_longitude": "38.76",
                    "guest_phone_number": "+15551239999",
                },
                format="json",
            )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        ride_id = response.data["id"]

        fare = Fare.objects.get(ride_id=ride_id, fare_type=FareType.ESTIMATE)
        self.assertEqual(fare.total_amount, Decimal("5.50"))
