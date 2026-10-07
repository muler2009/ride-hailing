from datetime import date, timedelta
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from apps.analytics.models import DailyPlatformRollup, DailyVehicleTypeRollup
from apps.analytics.services import (
    compute_daily_rollup,
    compute_daily_rollups_for_range,
    get_operations_summary,
    get_revenue_summary,
)
from apps.dispatch.models import DispatchOffer, DispatchOfferStatus
from apps.dispatch.services import create_offers
from apps.drivers.services import apply_as_driver, approve_driver
from apps.earnings.models import DriverEarning
from apps.identity.services import assign_role
from apps.payments.services import confirm_cash_payment, initiate_payment
from apps.pricing.services import estimate_fare_for_ride
from apps.rides.models import Ride, RideStatus
from apps.rides.services import accept_ride, complete_trip, create_ride, mark_arrived, mark_arriving, start_trip
from apps.users.models import User
from apps.vehicles.models import VehicleType
from apps.vehicles.services import register_vehicle

YESTERDAY = timezone.localdate() - timedelta(days=1)
TWO_DAYS_AGO = timezone.localdate() - timedelta(days=2)


def _ride_kwargs(**overrides):
    base = dict(
        vehicle_type=VehicleType.objects.get(name="Sedan"),
        pickup_address="A", pickup_latitude="9.0300", pickup_longitude="38.7400",
        destination_address="B", destination_latitude="9.0100", destination_longitude="38.7600",
    )
    base.update(overrides)
    return base


def _backdate(obj, target_date, hour=12):
    """Sets created_at (and, if present, responded_at) to a fixed instant on target_date. See apps/dispatch/tests.py for the same pattern."""
    obj.created_at = timezone.make_aware(timezone.datetime.combine(target_date, timezone.datetime.min.time())) + timedelta(hours=hour)
    obj.save(update_fields=["created_at"])
    return obj


def _make_driver(tag, *, approved=True):
    user = User.objects.create(email=f"an-driver-{tag}@example.com", keycloak_id=f"kc-an-driver-{tag}")
    admin = User.objects.create(email=f"an-approver-{tag}@example.com", keycloak_id=f"kc-an-approver-{tag}")
    driver = apply_as_driver(user, license_number=f"AN-{tag}", license_expiry="2030-01-01")
    if approved:
        approve_driver(driver, admin)
        register_vehicle(
            driver, vehicle_type=VehicleType.objects.get(name="Sedan"),
            make="Toyota", model="Corolla", year=2022, license_plate=f"AN-{tag}",
        )
    return driver, admin


def _paid_ride(tag, *, on_date=None, vehicle_type_name="Sedan"):
    """A ride taken end-to-end to PAID, with every timestamped row (ride, offer, earning) backdated to on_date."""
    driver, admin = _make_driver(tag)
    if vehicle_type_name != "Sedan":
        vt = VehicleType.objects.get(name=vehicle_type_name)
        driver.vehicles.update(vehicle_type=vt)
    rider = User.objects.create(email=f"an-rider-{tag}@example.com", keycloak_id=f"kc-an-rider-{tag}")
    ride = create_ride(rider=rider, **_ride_kwargs(vehicle_type=VehicleType.objects.get(name=vehicle_type_name)))
    ride.estimated_distance_km = Decimal("5.0")
    ride.estimated_duration_minutes = Decimal("10.0")
    ride.save()
    estimate_fare_for_ride(ride)
    offers = create_offers(ride, [(driver, 0.3)])
    accept_ride(ride.id, driver)
    mark_arriving(ride.id, driver)
    mark_arrived(ride.id, driver)
    start_trip(ride.id, driver)
    complete_trip(ride.id, driver)
    ride.refresh_from_db()
    initiate_payment(ride, method="CASH")
    ride.refresh_from_db()
    confirm_cash_payment(ride, driver)
    ride.refresh_from_db()

    if on_date is not None:
        _backdate(ride, on_date)
        for offer in offers:
            _backdate(offer, on_date)
        DriverEarning.objects.filter(ride=ride).update(
            created_at=timezone.make_aware(timezone.datetime.combine(on_date, timezone.datetime.min.time())) + timedelta(hours=12)
        )
    return ride, rider, driver, admin


class ComputeDailyRollupTests(TestCase):
    def test_refuses_to_compute_today_or_a_future_day(self):
        with self.assertRaises(ValueError):
            compute_daily_rollup(timezone.localdate())
        with self.assertRaises(ValueError):
            compute_daily_rollup(timezone.localdate() + timedelta(days=1))

    def test_empty_day_returns_zeroes_and_null_rates_not_errors(self):
        rollup = compute_daily_rollup(YESTERDAY)
        self.assertEqual(rollup.rides_requested, 0)
        self.assertEqual(rollup.gross_revenue, 0)
        self.assertIsNone(rollup.average_fare)
        self.assertIsNone(rollup.offer_acceptance_rate)
        self.assertIsNone(rollup.no_driver_found_rate)

    def test_ride_outcome_counts(self):
        rider = User.objects.create(email="an-outcomes@example.com", keycloak_id="kc-an-outcomes")
        requested = create_ride(rider=rider, **_ride_kwargs())
        _backdate(requested, YESTERDAY)

        cancelled = create_ride(
            rider=User.objects.create(email="an-outcomes2@example.com", keycloak_id="kc-an-outcomes2"),
            **_ride_kwargs(),
        )
        cancelled.status = RideStatus.CANCELLED_BY_RIDER
        cancelled.save()
        _backdate(cancelled, YESTERDAY)

        no_driver = create_ride(
            rider=User.objects.create(email="an-outcomes3@example.com", keycloak_id="kc-an-outcomes3"),
            **_ride_kwargs(),
        )
        no_driver.status = RideStatus.NO_DRIVER_FOUND
        no_driver.save()
        _backdate(no_driver, YESTERDAY)

        rollup = compute_daily_rollup(YESTERDAY)
        self.assertEqual(rollup.rides_requested, 3)
        self.assertEqual(rollup.rides_cancelled, 1)
        self.assertEqual(rollup.rides_no_driver_found, 1)

    def test_data_outside_the_day_window_is_excluded(self):
        rider = User.objects.create(email="an-window@example.com", keycloak_id="kc-an-window")
        before = create_ride(rider=rider, **_ride_kwargs())
        _backdate(before, TWO_DAYS_AGO)
        after = create_ride(
            rider=User.objects.create(email="an-window2@example.com", keycloak_id="kc-an-window2"),
            **_ride_kwargs(),
        )
        # "after" defaults to created_at = now (today), well outside yesterday's window.

        rollup = compute_daily_rollup(YESTERDAY)
        self.assertEqual(rollup.rides_requested, 0)

    def test_revenue_matches_a_manual_query_against_raw_data(self):
        """The roadmap's own testable criterion for this phase, applied directly."""
        ride, _rider, _driver, _admin = _paid_ride(1, on_date=YESTERDAY)
        earning = DriverEarning.objects.get(ride=ride)

        rollup = compute_daily_rollup(YESTERDAY)

        self.assertEqual(rollup.rides_with_earning, 1)
        self.assertEqual(rollup.gross_revenue, earning.fare_amount)
        self.assertEqual(rollup.platform_commission, earning.commission_amount)
        self.assertEqual(rollup.driver_earnings, earning.driver_earning_amount)
        self.assertEqual(rollup.average_fare, earning.fare_amount)
        self.assertEqual(rollup.gross_revenue, rollup.platform_commission + rollup.driver_earnings)

    def test_vehicle_type_breakdown_reconciles_with_the_total(self):
        _paid_ride(2, on_date=YESTERDAY, vehicle_type_name="Sedan")
        _paid_ride(3, on_date=YESTERDAY, vehicle_type_name="SUV")

        rollup = compute_daily_rollup(YESTERDAY)
        breakdown = DailyVehicleTypeRollup.objects.filter(date=YESTERDAY)

        self.assertEqual(breakdown.count(), 2)
        self.assertEqual(sum(row.gross_revenue for row in breakdown), rollup.gross_revenue)
        self.assertEqual(sum(row.ride_count for row in breakdown), rollup.rides_with_earning)

    def test_active_drivers_counts_distinct_drivers_not_rides(self):
        driver, admin = _make_driver(4)
        for i in range(2):
            rider = User.objects.create(email=f"an-multi-{i}@example.com", keycloak_id=f"kc-an-multi-{i}")
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
            complete_trip(ride.id, driver)
            ride.refresh_from_db()
            _backdate(ride, YESTERDAY)

        rollup = compute_daily_rollup(YESTERDAY)
        self.assertEqual(rollup.rides_completed, 2)
        self.assertEqual(rollup.active_drivers, 1)

    def test_dispatch_offer_counts_and_acceptance_rate(self):
        driver, _admin = _make_driver(5)
        rider = User.objects.create(email="an-dispatch@example.com", keycloak_id="kc-an-dispatch")
        ride = create_ride(rider=rider, **_ride_kwargs())
        offers = create_offers(ride, [(driver, 0.3)])
        accept_ride(ride.id, driver)
        for offer in offers:
            _backdate(DispatchOffer.objects.get(pk=offer.pk), YESTERDAY)

        rollup = compute_daily_rollup(YESTERDAY)
        self.assertEqual(rollup.offers_sent, 1)
        self.assertEqual(rollup.offers_accepted, 1)
        self.assertEqual(rollup.offer_acceptance_rate, 1.0)

    def test_recompute_is_idempotent_not_additive(self):
        _paid_ride(6, on_date=YESTERDAY)

        first = compute_daily_rollup(YESTERDAY)
        second = compute_daily_rollup(YESTERDAY)

        self.assertEqual(first.id, second.id)  # same row, updated in place — not a duplicate
        self.assertEqual(DailyPlatformRollup.objects.filter(date=YESTERDAY).count(), 1)
        self.assertEqual(second.rides_with_earning, 1)
        self.assertEqual(DailyVehicleTypeRollup.objects.filter(date=YESTERDAY).count(), 1)

    def test_recompute_drops_a_vehicle_type_that_no_longer_has_rides(self):
        """A rerun must not leave a stale breakdown row behind (see the model's docstring)."""
        ride, _r, _d, _a = _paid_ride(7, on_date=YESTERDAY, vehicle_type_name="Sedan")
        compute_daily_rollup(YESTERDAY)
        self.assertEqual(DailyVehicleTypeRollup.objects.filter(date=YESTERDAY).count(), 1)

        DriverEarning.objects.filter(ride=ride).delete()
        compute_daily_rollup(YESTERDAY)
        self.assertEqual(DailyVehicleTypeRollup.objects.filter(date=YESTERDAY).count(), 0)


class BackfillRangeTests(TestCase):
    def test_computes_one_row_per_day_inclusive(self):
        start = YESTERDAY - timedelta(days=2)
        results = compute_daily_rollups_for_range(start, YESTERDAY)
        self.assertEqual(len(results), 3)
        self.assertEqual(DailyPlatformRollup.objects.filter(date__gte=start, date__lte=YESTERDAY).count(), 3)

    def test_start_after_end_raises(self):
        with self.assertRaises(ValueError):
            compute_daily_rollups_for_range(YESTERDAY, YESTERDAY - timedelta(days=1))


class SummaryServiceTests(TestCase):
    def test_operations_summary_sums_counts_and_weights_the_rate_by_volume(self):
        # Day 1: 1 offer sent, 1 accepted. Day 2: 3 offers sent, 0 accepted.
        # A naive average-of-daily-rates would say 50%; the correct
        # volume-weighted rate across 4 sent / 1 accepted is 25%.
        driver, _admin = _make_driver(10)
        rider1 = User.objects.create(email="an-sum1@example.com", keycloak_id="kc-an-sum1")
        ride1 = create_ride(rider=rider1, **_ride_kwargs())
        offers1 = create_offers(ride1, [(driver, 0.3)])
        accept_ride(ride1.id, driver)
        for o in offers1:
            _backdate(DispatchOffer.objects.get(pk=o.pk), TWO_DAYS_AGO)

        other_drivers = [_make_driver(f"10-{i}")[0] for i in range(3)]
        rider2 = User.objects.create(email="an-sum2@example.com", keycloak_id="kc-an-sum2")
        ride2 = create_ride(rider=rider2, **_ride_kwargs())
        offers2 = create_offers(ride2, [(d, 0.3) for d in other_drivers])
        for o in offers2:
            _backdate(DispatchOffer.objects.get(pk=o.pk), YESTERDAY)

        compute_daily_rollup(TWO_DAYS_AGO)
        compute_daily_rollup(YESTERDAY)

        summary = get_operations_summary(days=2)
        self.assertEqual(summary["offers_sent"], 4)
        self.assertEqual(summary["offers_accepted"], 1)
        self.assertEqual(summary["offer_acceptance_rate"], 0.25)

    def test_revenue_summary_sums_across_days_and_vehicle_types(self):
        _paid_ride(11, on_date=TWO_DAYS_AGO, vehicle_type_name="Sedan")
        _paid_ride(12, on_date=YESTERDAY, vehicle_type_name="Sedan")
        compute_daily_rollup(TWO_DAYS_AGO)
        compute_daily_rollup(YESTERDAY)

        summary = get_revenue_summary(days=2)
        self.assertEqual(summary["ride_count"], 2)
        self.assertEqual(summary["gross_revenue"], summary["platform_commission"] + summary["driver_earnings"])
        self.assertEqual(len(summary["by_vehicle_type"]), 1)
        self.assertEqual(summary["by_vehicle_type"][0]["ride_count"], 2)

    def test_days_available_reflects_how_much_history_actually_exists(self):
        compute_daily_rollup(YESTERDAY)  # only 1 of the last 30 days has been rolled up
        summary = get_operations_summary(days=30)
        self.assertEqual(summary["days_available"], 1)
        self.assertEqual(summary["window_days"], 30)


class AnalyticsApiPermissionTests(APITestCase):
    def setUp(self):
        self.ops = User.objects.create(email="an-ops@example.com", keycloak_id="kc-an-ops")
        assign_role(self.ops, "Operations Manager")
        self.finance = User.objects.create(email="an-finance@example.com", keycloak_id="kc-an-finance")
        assign_role(self.finance, "Finance Admin")
        self.rider = User.objects.create(email="an-plain-rider@example.com", keycloak_id="kc-an-plain-rider")
        assign_role(self.rider, "Rider")

        _paid_ride(20, on_date=YESTERDAY)
        compute_daily_rollup(YESTERDAY)

    def test_ops_manager_can_read_operations_endpoints(self):
        self.client.force_authenticate(user=self.ops)
        self.assertEqual(self.client.get(reverse("analytics_operations_daily")).status_code, status.HTTP_200_OK)
        self.assertEqual(self.client.get(reverse("analytics_operations_summary")).status_code, status.HTTP_200_OK)

    def test_ops_manager_cannot_read_revenue_endpoints(self):
        """Same permission-specific gating as Phase 12: monitoring rides isn't a finance capability."""
        self.client.force_authenticate(user=self.ops)
        self.assertEqual(self.client.get(reverse("analytics_revenue_daily")).status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(self.client.get(reverse("analytics_revenue_summary")).status_code, status.HTTP_403_FORBIDDEN)

    def test_finance_admin_can_read_revenue_endpoints(self):
        self.client.force_authenticate(user=self.finance)
        response = self.client.get(reverse("analytics_revenue_daily"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("gross_revenue", response.data["results"][0])

        summary = self.client.get(reverse("analytics_revenue_summary"))
        self.assertEqual(summary.status_code, status.HTTP_200_OK)
        self.assertIn("by_vehicle_type", summary.data)

    def test_rider_is_forbidden_everywhere(self):
        self.client.force_authenticate(user=self.rider)
        for url_name in ["analytics_operations_daily", "analytics_operations_summary", "analytics_revenue_daily", "analytics_revenue_summary"]:
            self.assertEqual(self.client.get(reverse(url_name)).status_code, status.HTTP_403_FORBIDDEN)

    def test_unauthenticated_is_rejected(self):
        response = self.client.get(reverse("analytics_operations_daily"))
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_days_parameter_is_clamped(self):
        self.client.force_authenticate(user=self.ops)
        response = self.client.get(reverse("analytics_operations_summary"), {"days": "99999"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["window_days"], 365)

    def test_garbage_days_parameter_falls_back_to_default(self):
        self.client.force_authenticate(user=self.ops)
        response = self.client.get(reverse("analytics_operations_summary"), {"days": "not-a-number"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["window_days"], 30)

    def test_daily_list_is_paginated_and_newest_first(self):
        compute_daily_rollup(TWO_DAYS_AGO)
        self.client.force_authenticate(user=self.ops)
        response = self.client.get(reverse("analytics_operations_daily"), {"days": 5})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        dates = [row["date"] for row in response.data["results"]]
        self.assertEqual(dates, sorted(dates, reverse=True))
