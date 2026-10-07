import threading
from decimal import Decimal

from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from apps.dispatch.services import create_offers
from apps.drivers.services import apply_as_driver, approve_driver
from apps.earnings.models import DriverEarning, Payout, PayoutStatus, Wallet, WalletLedgerEntryType
from apps.earnings.services import (
    EarningsError,
    credit_driver_earning,
    get_wallet_balance,
    mark_payout_completed,
    mark_payout_failed,
    request_payout,
)
from apps.identity.services import assign_role
from apps.pricing.services import estimate_fare_for_ride
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


def _paid_ride():
    n = User.objects.count()
    rider = User.objects.create(email=f"earn-rider-{n}@example.com", keycloak_id=f"kc-earn-rider-{n}")
    driver_user = User.objects.create(email=f"earn-driver-{n}@example.com", keycloak_id=f"kc-earn-driver-{n}")
    admin = User.objects.create(email=f"earn-admin-{n}@example.com", keycloak_id=f"kc-earn-admin-{n}")
    driver = apply_as_driver(driver_user, license_number=f"D-{driver_user.id}", license_expiry="2030-01-01")
    approve_driver(driver, admin)
    sedan = VehicleType.objects.get(name="Sedan")
    register_vehicle(driver, vehicle_type=sedan, make="Toyota", model="Corolla", year=2022, license_plate=f"EARN-{driver.id}")

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
    ride = complete_trip(ride.id, driver)  # -> PAYMENT_PENDING, FINAL fare created

    from apps.payments.services import confirm_cash_payment, initiate_payment

    initiate_payment(ride, method="CASH")
    ride.refresh_from_db()
    confirm_cash_payment(ride, driver)  # -> PAID, credit_driver_earning fires automatically
    ride.refresh_from_db()
    return ride, driver


class CreditDriverEarningTests(TestCase):
    def test_paying_a_ride_automatically_credits_the_driver(self):
        ride, driver = _paid_ride()
        earning = DriverEarning.objects.get(ride=ride)

        fare = ride.fares.get(fare_type="FINAL")
        expected_commission = (fare.total_amount * Decimal("0.20")).quantize(Decimal("0.01"))
        self.assertEqual(earning.commission_amount, expected_commission)
        self.assertEqual(earning.driver_earning_amount, fare.total_amount - expected_commission)

    def test_wallet_balance_equals_sum_of_ledger_entries(self):
        ride, driver = _paid_ride()
        wallet = Wallet.objects.get(driver=driver)
        balance = get_wallet_balance(wallet)

        manual_sum = sum(e.amount for e in wallet.ledger_entries.all())
        self.assertEqual(balance, manual_sum)

        earning = DriverEarning.objects.get(ride=ride)
        self.assertEqual(balance, earning.driver_earning_amount)

    def test_crediting_the_same_ride_twice_does_not_double_credit(self):
        """The core FR-EAR-05 guarantee: a retried trigger must not double-credit."""
        ride, driver = _paid_ride()
        wallet = Wallet.objects.get(driver=driver)
        balance_after_first = get_wallet_balance(wallet)

        second = credit_driver_earning(ride)  # simulates a retried trigger

        self.assertEqual(DriverEarning.objects.filter(ride=ride).count(), 1)
        self.assertEqual(second.id, DriverEarning.objects.get(ride=ride).id)
        self.assertEqual(get_wallet_balance(wallet), balance_after_first)

    def test_credit_without_a_final_fare_raises(self):
        rider = User.objects.create(email="no-fare-rider@example.com", keycloak_id="kc-no-fare-rider")
        ride = create_ride(rider=rider, **_ride_kwargs())  # no FINAL fare yet
        with self.assertRaises(EarningsError):
            credit_driver_earning(ride)


@override_settings(MINIMUM_PAYOUT_AMOUNT=Decimal("1.00"))
class PayoutRequestTests(TestCase):
    def test_can_request_a_payout_up_to_the_full_balance(self):
        ride, driver = _paid_ride()
        payout = request_payout(driver)

        wallet = Wallet.objects.get(driver=driver)
        self.assertEqual(get_wallet_balance(wallet), Decimal("0.00"))
        self.assertEqual(payout.status, PayoutStatus.PENDING)

    def test_cannot_request_below_minimum_payout(self):
        ride, driver = _paid_ride()
        with self.assertRaises(EarningsError):
            request_payout(driver, amount=Decimal("0.50"))

    def test_cannot_request_more_than_available_balance(self):
        ride, driver = _paid_ride()
        wallet = Wallet.objects.get(driver=driver)
        balance = get_wallet_balance(wallet)
        with self.assertRaises(EarningsError):
            request_payout(driver, amount=balance + Decimal("100.00"))

    def test_cannot_have_two_pending_payouts_at_once(self):
        ride, driver = _paid_ride()
        request_payout(driver, amount=Decimal("1.00"))
        with self.assertRaises(EarningsError):
            request_payout(driver, amount=Decimal("1.00"))

    def test_admin_completes_a_payout(self):
        ride, driver = _paid_ride()
        payout = request_payout(driver)
        admin = User.objects.create(email="payout-admin@example.com", keycloak_id="kc-payout-admin")

        completed = mark_payout_completed(payout, admin)
        self.assertEqual(completed.status, PayoutStatus.COMPLETED)
        self.assertEqual(completed.processed_by, admin)

    def test_admin_failing_a_payout_reverses_the_debit(self):
        ride, driver = _paid_ride()
        wallet = Wallet.objects.get(driver=driver)
        balance_before = get_wallet_balance(wallet)

        payout = request_payout(driver)
        admin = User.objects.create(email="payout-admin2@example.com", keycloak_id="kc-payout-admin2")
        mark_payout_failed(payout, admin, reason="Bank account invalid")

        self.assertEqual(get_wallet_balance(wallet), balance_before)  # fully reversed
        reversal = wallet.ledger_entries.get(entry_type=WalletLedgerEntryType.CREDIT_PAYOUT_REVERSAL)
        self.assertEqual(reversal.amount, payout.amount)

    def test_cannot_complete_an_already_completed_payout(self):
        ride, driver = _paid_ride()
        payout = request_payout(driver)
        admin = User.objects.create(email="payout-admin3@example.com", keycloak_id="kc-payout-admin3")
        mark_payout_completed(payout, admin)

        with self.assertRaises(EarningsError):
            mark_payout_completed(payout, admin)


@override_settings(MINIMUM_PAYOUT_AMOUNT=Decimal("1.00"))
class ConcurrentPayoutRequestTests(TransactionTestCase):
    """
    Real threads, real DB connections — proves select_for_update on the
    Wallet genuinely serializes two concurrent payout requests rather than
    letting both drain the same balance.
    """

    def setUp(self):
        from django.core.management import call_command

        call_command("seed_rbac", verbosity=0)
        call_command("seed_vehicle_types", verbosity=0)
        call_command("seed_pricing_rules", verbosity=0)

    def tearDown(self):
        from django.core.management import call_command

        call_command("seed_rbac", verbosity=0)
        call_command("seed_vehicle_types", verbosity=0)
        call_command("seed_pricing_rules", verbosity=0)

    def test_only_one_of_two_concurrent_full_balance_payouts_can_succeed(self):
        ride, driver = _paid_ride()
        wallet = Wallet.objects.get(driver=driver)
        balance = get_wallet_balance(wallet)
        self.assertGreater(balance, 0)

        outcomes = {}

        def try_payout(key):
            try:
                request_payout(driver, amount=balance)
                outcomes[key] = "won"
            except Exception as exc:
                outcomes[key] = f"lost: {exc}"

        t1 = threading.Thread(target=try_payout, args=("t1",))
        t2 = threading.Thread(target=try_payout, args=("t2",))
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        winners = [k for k, v in outcomes.items() if v == "won"]
        self.assertEqual(len(winners), 1, f"expected exactly one winner, got: {outcomes}")
        self.assertEqual(Payout.objects.filter(driver=driver).count(), 1)
        self.assertEqual(get_wallet_balance(wallet), Decimal("0.00"))


class EarningsApiTests(APITestCase):
    def setUp(self):
        self.finance_admin = User.objects.create(email="earn-api-admin@example.com", keycloak_id="kc-earn-api-admin")
        assign_role(self.finance_admin, "Finance Admin")

    def test_driver_can_view_own_wallet(self):
        ride, driver = _paid_ride()
        self.client.force_authenticate(user=driver.user)

        response = self.client.get(reverse("my_wallet"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertGreater(len(response.data["ledger_entries"]), 0)

    def test_driver_can_view_own_earnings(self):
        ride, driver = _paid_ride()
        self.client.force_authenticate(user=driver.user)

        response = self.client.get(reverse("my_earnings"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)

    @override_settings(MINIMUM_PAYOUT_AMOUNT=Decimal("1.00"))
    def test_driver_can_request_and_view_payout(self):
        ride, driver = _paid_ride()
        self.client.force_authenticate(user=driver.user)

        response = self.client.post(reverse("my_payouts"), {}, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["status"], "PENDING")

        list_response = self.client.get(reverse("my_payouts"))
        self.assertEqual(len(list_response.data), 1)

    def test_non_driver_cannot_access_wallet(self):
        rider = User.objects.create(email="not-a-driver@example.com", keycloak_id="kc-not-a-driver")
        self.client.force_authenticate(user=rider)
        response = self.client.get(reverse("my_wallet"))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @override_settings(MINIMUM_PAYOUT_AMOUNT=Decimal("1.00"))
    def test_finance_admin_can_list_and_complete_payouts(self):
        ride, driver = _paid_ride()
        self.client.force_authenticate(user=driver.user)
        self.client.post(reverse("my_payouts"), {}, format="json")
        payout = Payout.objects.get(driver=driver)

        self.client.force_authenticate(user=self.finance_admin)
        list_response = self.client.get(reverse("admin_payout_list"), {"status": "PENDING"})
        self.assertEqual(len(list_response.data["results"]), 1)

        complete_response = self.client.post(reverse("admin_payout_complete", args=[payout.id]))
        self.assertEqual(complete_response.status_code, status.HTTP_200_OK)
        self.assertEqual(complete_response.data["status"], "COMPLETED")

    @override_settings(MINIMUM_PAYOUT_AMOUNT=Decimal("1.00"))
    def test_driver_cannot_process_payouts(self):
        ride, driver = _paid_ride()
        self.client.force_authenticate(user=driver.user)
        self.client.post(reverse("my_payouts"), {}, format="json")
        payout = Payout.objects.get(driver=driver)

        response = self.client.post(reverse("admin_payout_complete", args=[payout.id]))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @override_settings(MINIMUM_PAYOUT_AMOUNT=Decimal("1.00"))
    def test_finance_admin_can_fail_a_payout_with_reason(self):
        ride, driver = _paid_ride()
        self.client.force_authenticate(user=driver.user)
        self.client.post(reverse("my_payouts"), {}, format="json")
        payout = Payout.objects.get(driver=driver)

        self.client.force_authenticate(user=self.finance_admin)
        response = self.client.post(
            reverse("admin_payout_fail", args=[payout.id]), {"reason": "Invalid bank details"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["status"], "FAILED")
        self.assertEqual(response.data["failure_reason"], "Invalid bank details")
