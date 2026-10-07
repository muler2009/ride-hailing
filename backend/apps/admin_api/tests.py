from decimal import Decimal

from django.test import TestCase
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from apps.admin_api.models import AuditLog
from apps.admin_api.services import get_dashboard_summary, get_dispatch_health, get_live_rides, get_revenue_report
from apps.dispatch.services import create_offers
from apps.drivers.services import apply_as_driver, approve_driver, reject_driver, set_availability, suspend_driver
from apps.earnings.services import mark_payout_completed, request_payout
from apps.identity.services import assign_role
from apps.locations import geo
from apps.payments.services import confirm_cash_payment, initiate_payment, refund_payment
from apps.payments.models import Payment
from apps.pricing.services import estimate_fare_for_ride
from apps.rides.models import RideStatus
from apps.rides.services import accept_ride, complete_trip, create_ride, mark_arrived, mark_arriving, start_trip
from apps.users.models import User
from apps.vehicles.models import VehicleType
from apps.vehicles.services import register_vehicle


def _flush_geo():
    client = geo.get_redis_client()
    client.delete(geo.GEO_KEY)
    for key in client.keys("locations:driver_seen:*"):
        client.delete(key)


def _ride_kwargs(**overrides):
    base = dict(
        vehicle_type=VehicleType.objects.get(name="Sedan"),
        pickup_address="A", pickup_latitude="9.0300", pickup_longitude="38.7400",
        destination_address="B", destination_latitude="9.0100", destination_longitude="38.7600",
    )
    base.update(overrides)
    return base


def _make_driver(tag, *, approved=True):
    user = User.objects.create(email=f"adm-driver-{tag}@example.com", keycloak_id=f"kc-adm-driver-{tag}")
    admin = User.objects.create(email=f"adm-approver-{tag}@example.com", keycloak_id=f"kc-adm-approver-{tag}")
    driver = apply_as_driver(user, license_number=f"D-{tag}", license_expiry="2030-01-01")
    if approved:
        approve_driver(driver, admin)
        register_vehicle(
            driver, vehicle_type=VehicleType.objects.get(name="Sedan"),
            make="Toyota", model="Corolla", year=2022, license_plate=f"ADM-{tag}",
        )
    return driver, admin


def _paid_ride(tag):
    driver, admin = _make_driver(tag)
    rider = User.objects.create(email=f"adm-rider-{tag}@example.com", keycloak_id=f"kc-adm-rider-{tag}")
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
    initiate_payment(ride, method="CASH")
    ride.refresh_from_db()
    confirm_cash_payment(ride, driver)
    ride.refresh_from_db()
    return ride, rider, driver, admin


class DashboardSummaryTests(TestCase):
    def test_empty_platform_returns_zeroes_not_nulls(self):
        summary = get_dashboard_summary()
        self.assertEqual(summary["active_rides"], 0)
        self.assertEqual(summary["gross_revenue"], 0)
        self.assertEqual(summary["platform_commission"], 0)

    def test_counts_active_rides_and_online_drivers(self):
        _flush_geo()
        self.addCleanup(_flush_geo)
        driver, _admin = _make_driver(1)
        set_availability(driver, True)
        rider = User.objects.create(email="adm-active@example.com", keycloak_id="kc-adm-active")
        create_ride(rider=rider, **_ride_kwargs())  # SEARCHING_DRIVER — active

        summary = get_dashboard_summary()
        self.assertEqual(summary["active_rides"], 1)
        self.assertEqual(summary["online_drivers"], 1)

    def test_completed_paid_ride_contributes_revenue_and_commission(self):
        ride, _rider, _driver, _admin = _paid_ride(2)
        fare = ride.fares.get(fare_type="FINAL")

        summary = get_dashboard_summary()
        self.assertEqual(summary["gross_revenue"], fare.total_amount)
        self.assertGreater(summary["platform_commission"], 0)
        self.assertEqual(summary["rides_completed"], 1)

    def test_pending_driver_review_is_surfaced(self):
        _make_driver(3, approved=False)
        summary = get_dashboard_summary()
        self.assertEqual(summary["drivers_pending_review"], 1)

    def test_pending_payouts_are_surfaced(self):
        ride, _rider, driver, _admin = _paid_ride(4)
        from django.test import override_settings

        with override_settings(MINIMUM_PAYOUT_AMOUNT=Decimal("1.00")):
            request_payout(driver)

        summary = get_dashboard_summary()
        self.assertEqual(summary["payouts_pending"], 1)
        self.assertGreater(summary["payouts_pending_amount"], 0)


class LiveRidesTests(TestCase):
    def setUp(self):
        _flush_geo()
        self.addCleanup(_flush_geo)

    def test_in_flight_ride_appears_with_driver_position(self):
        driver, _admin = _make_driver(10)
        set_availability(driver, True)
        geo.set_driver_location(driver.id, latitude=9.0320, longitude=38.7420)

        rider = User.objects.create(email="adm-live@example.com", keycloak_id="kc-adm-live")
        ride = create_ride(rider=rider, **_ride_kwargs())
        create_offers(ride, [(driver, 0.3)])
        accept_ride(ride.id, driver)

        live = get_live_rides()
        self.assertEqual(len(live), 1)
        self.assertEqual(live[0]["ride_id"], str(ride.id))
        self.assertAlmostEqual(live[0]["driver_latitude"], 9.0320, places=3)

    def test_completed_ride_does_not_appear(self):
        ride, _rider, _driver, _admin = _paid_ride(11)
        self.assertEqual(ride.status, RideStatus.PAID)
        self.assertEqual(get_live_rides(), [])

    def test_guest_ride_is_labelled_without_an_email(self):
        create_ride(guest_phone_number="+15551110000", **_ride_kwargs())
        live = get_live_rides()
        self.assertEqual(len(live), 1)
        self.assertTrue(live[0]["rider"].startswith("guest:"))

    def test_unassigned_ride_has_null_driver_position(self):
        rider = User.objects.create(email="adm-live2@example.com", keycloak_id="kc-adm-live2")
        create_ride(rider=rider, **_ride_kwargs())
        live = get_live_rides()
        self.assertIsNone(live[0]["driver"])
        self.assertIsNone(live[0]["driver_latitude"])


class RevenueAndDispatchHealthTests(TestCase):
    def test_revenue_report_splits_commission_and_driver_share(self):
        ride, _rider, _driver, _admin = _paid_ride(20)
        report = get_revenue_report()

        self.assertEqual(report["ride_count"], 1)
        self.assertEqual(
            report["gross_revenue"], report["platform_commission"] + report["driver_earnings"]
        )

    def test_revenue_report_breaks_down_by_vehicle_type(self):
        _paid_ride(21)
        report = get_revenue_report()
        self.assertEqual(len(report["by_vehicle_type"]), 1)
        self.assertEqual(report["by_vehicle_type"][0]["vehicle_type__name"], "Sedan")

    def test_dispatch_health_reports_acceptance_rate(self):
        _flush_geo()
        self.addCleanup(_flush_geo)
        driver, _admin = _make_driver(22)
        rider = User.objects.create(email="adm-dh@example.com", keycloak_id="kc-adm-dh")
        ride = create_ride(rider=rider, **_ride_kwargs())
        create_offers(ride, [(driver, 0.3)])
        accept_ride(ride.id, driver)

        health = get_dispatch_health()
        self.assertEqual(health["offers_sent"], 1)
        self.assertEqual(health["offers_accepted"], 1)
        self.assertEqual(health["offer_acceptance_rate"], 1.0)

    def test_dispatch_health_handles_zero_activity_without_dividing_by_zero(self):
        health = get_dispatch_health()
        self.assertIsNone(health["offer_acceptance_rate"])
        self.assertIsNone(health["no_driver_found_rate"])


class AuditTrailTests(TestCase):
    """FR-ADM-06 — administrative actions across every app must leave a trail."""

    def test_driver_approval_is_audited(self):
        driver, admin = _make_driver(30)
        entry = AuditLog.objects.get(action="DRIVER_APPROVED", target_id=str(driver.id))
        self.assertEqual(entry.actor, admin)
        self.assertEqual(entry.actor_email, admin.email)
        self.assertEqual(entry.target_type, "Driver")

    def test_driver_suspension_records_the_reason(self):
        driver, admin = _make_driver(31)
        suspend_driver(driver, admin, "policy violation")

        entry = AuditLog.objects.get(action="DRIVER_SUSPENDED", target_id=str(driver.id))
        self.assertEqual(entry.reason, "policy violation")

    def test_driver_rejection_is_audited(self):
        driver, admin = _make_driver(32, approved=False)
        reject_driver(driver, admin, "expired license")
        self.assertTrue(AuditLog.objects.filter(action="DRIVER_REJECTED", target_id=str(driver.id)).exists())

    def test_refund_is_audited_with_amount(self):
        ride, _rider, _driver, admin = _paid_ride(33)
        payment = Payment.objects.get(ride=ride)
        refund_payment(payment, admin_user=admin, reason="rider complaint")

        entry = AuditLog.objects.get(action="PAYMENT_REFUNDED", target_id=str(payment.id))
        self.assertEqual(entry.reason, "rider complaint")
        self.assertIn("amount", entry.changes)

    def test_payout_completion_is_audited(self):
        ride, _rider, driver, admin = _paid_ride(34)
        from django.test import override_settings

        with override_settings(MINIMUM_PAYOUT_AMOUNT=Decimal("1.00")):
            payout = request_payout(driver)
        mark_payout_completed(payout, admin)

        self.assertTrue(AuditLog.objects.filter(action="PAYOUT_COMPLETED", target_id=str(payout.id)).exists())

    def test_actor_email_is_snapshotted_so_the_entry_survives_account_deletion(self):
        driver, admin = _make_driver(35)
        entry = AuditLog.objects.get(action="DRIVER_APPROVED", target_id=str(driver.id))
        admin_email = admin.email
        admin.delete(hard=True)

        entry.refresh_from_db()
        self.assertIsNone(entry.actor)  # FK nulled out
        self.assertEqual(entry.actor_email, admin_email)  # but the entry still reads correctly

    def test_audit_entry_survives_its_target_being_deleted(self):
        """target_id is a plain string precisely so this works."""
        driver, admin = _make_driver(36, approved=False)
        reject_driver(driver, admin, "test")
        driver_id = str(driver.id)
        driver.delete()

        entry = AuditLog.objects.get(action="DRIVER_REJECTED", target_id=driver_id)
        self.assertEqual(entry.target_id, driver_id)


class AdminApiPermissionTests(APITestCase):
    def setUp(self):
        _flush_geo()
        self.addCleanup(_flush_geo)
        self.ops = User.objects.create(email="ops@example.com", keycloak_id="kc-ops-adm")
        assign_role(self.ops, "Operations Manager")
        self.support = User.objects.create(email="support@example.com", keycloak_id="kc-support-adm")
        assign_role(self.support, "Support Agent")
        self.finance = User.objects.create(email="finance@example.com", keycloak_id="kc-finance-adm")
        assign_role(self.finance, "Finance Admin")
        self.superadmin = User.objects.create(email="super@example.com", keycloak_id="kc-super-adm")
        assign_role(self.superadmin, "Super Admin")
        self.rider = User.objects.create(email="plainrider@example.com", keycloak_id="kc-plain-adm")
        assign_role(self.rider, "Rider")

    def test_ops_manager_can_read_dashboard(self):
        self.client.force_authenticate(user=self.ops)
        response = self.client.get(reverse("admin_dashboard"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("active_rides", response.data)

    def test_rider_cannot_read_dashboard(self):
        self.client.force_authenticate(user=self.rider)
        response = self.client.get(reverse("admin_dashboard"))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_unauthenticated_is_rejected(self):
        response = self.client.get(reverse("admin_dashboard"))
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_support_agent_can_monitor_rides_but_not_see_revenue(self):
        """Permission-specific gating, not role tiers — a support agent monitoring rides isn't a finance role."""
        self.client.force_authenticate(user=self.support)
        self.assertEqual(self.client.get(reverse("admin_live_rides")).status_code, status.HTTP_200_OK)
        self.assertEqual(self.client.get(reverse("admin_revenue")).status_code, status.HTTP_403_FORBIDDEN)

    def test_finance_admin_can_read_revenue(self):
        self.client.force_authenticate(user=self.finance)
        response = self.client.get(reverse("admin_revenue"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("platform_commission", response.data)

    def test_ops_manager_cannot_read_audit_log(self):
        """The log holds operational roles accountable, so they can't read it."""
        self.client.force_authenticate(user=self.ops)
        response = self.client.get(reverse("admin_audit_log"))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_super_admin_can_read_audit_log(self):
        self.client.force_authenticate(user=self.superadmin)
        response = self.client.get(reverse("admin_audit_log"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_audit_log_can_be_filtered_by_action(self):
        driver, admin = _make_driver(40)
        suspend_driver(driver, admin, "reason")

        self.client.force_authenticate(user=self.superadmin)
        response = self.client.get(reverse("admin_audit_log"), {"action": "DRIVER_SUSPENDED"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        actions = {row["action"] for row in response.data["results"]}
        self.assertEqual(actions, {"DRIVER_SUSPENDED"})

    def test_window_parameter_is_clamped(self):
        self.client.force_authenticate(user=self.ops)
        response = self.client.get(reverse("admin_dashboard"), {"hours": "99999"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["window_hours"], 720)  # clamped to the maximum

    def test_garbage_window_parameter_falls_back_to_default(self):
        self.client.force_authenticate(user=self.ops)
        response = self.client.get(reverse("admin_dashboard"), {"hours": "not-a-number"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["window_hours"], 24)
