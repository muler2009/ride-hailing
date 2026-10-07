import hashlib
import hmac
import json
from decimal import Decimal
from unittest import mock

from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from apps.dispatch.services import create_offers
from apps.drivers.services import apply_as_driver, approve_driver
from apps.identity.services import assign_role
from apps.payments.factory import clear_provider_cache
from apps.payments.models import Payment, PaymentMethod, PaymentStatus, PaymentTransaction, PaymentTransactionType
from apps.payments.providers.base import PaymentProviderError
from apps.payments.providers.chapa_provider import ChapaProvider
from apps.payments.providers.stripe_provider import StripeProvider
from apps.payments.services import PaymentError, confirm_cash_payment, initiate_payment, process_webhook, refund_payment
from apps.pricing.services import estimate_fare_for_ride
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


def _completed_ride_with_final_fare():
    n = User.objects.count()
    rider = User.objects.create(email=f"pay-rider-{n}@example.com", keycloak_id=f"kc-pay-rider-{n}")
    driver_user = User.objects.create(email=f"pay-driver-{n}@example.com", keycloak_id=f"kc-pay-driver-{n}")
    admin = User.objects.create(email=f"pay-admin-{n}@example.com", keycloak_id=f"kc-pay-admin-{n}")
    driver = apply_as_driver(driver_user, license_number=f"D-{driver_user.id}", license_expiry="2030-01-01")
    approve_driver(driver, admin)
    sedan = VehicleType.objects.get(name="Sedan")
    register_vehicle(driver, vehicle_type=sedan, make="Toyota", model="Corolla", year=2022, license_plate=f"PAY-{driver.id}")

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
    return ride, rider, driver


class StripeWebhookSignatureTests(TestCase):
    """
    Pure cryptographic computation — no network involved — so this is
    tested for real, the same approach used for Keycloak's self-signed
    JWTs. A real signature is computed with a known secret and confirmed
    to actually validate (and a tampered one to actually fail).
    """

    def setUp(self):
        with override_settings(STRIPE_SECRET_KEY="sk_test_x", STRIPE_WEBHOOK_SECRET="whsec_test"):
            self.provider = StripeProvider()

    def _sign(self, payload: bytes, timestamp: str = "1700000000") -> str:
        signed_payload = f"{timestamp}.{payload.decode()}".encode()
        signature = hmac.new(b"whsec_test", signed_payload, hashlib.sha256).hexdigest()
        return f"t={timestamp},v1={signature}"

    def test_valid_signature_is_accepted(self):
        payload = json.dumps({"id": "evt_1", "type": "charge.succeeded"}).encode()
        header = self._sign(payload)
        self.assertTrue(self.provider.verify_webhook_signature(payload=payload, signature_header=header))

    def test_tampered_payload_is_rejected(self):
        payload = json.dumps({"id": "evt_1", "type": "charge.succeeded"}).encode()
        header = self._sign(payload)
        tampered = json.dumps({"id": "evt_1", "type": "charge.refunded"}).encode()
        self.assertFalse(self.provider.verify_webhook_signature(payload=tampered, signature_header=header))

    def test_wrong_secret_is_rejected(self):
        payload = json.dumps({"id": "evt_1"}).encode()
        signed_payload = f"1700000000.{payload.decode()}".encode()
        wrong_signature = hmac.new(b"wrong_secret", signed_payload, hashlib.sha256).hexdigest()
        header = f"t=1700000000,v1={wrong_signature}"
        self.assertFalse(self.provider.verify_webhook_signature(payload=payload, signature_header=header))

    def test_malformed_header_is_rejected_not_raised(self):
        payload = b"{}"
        self.assertFalse(self.provider.verify_webhook_signature(payload=payload, signature_header="garbage"))

    def test_parse_webhook_event_normalizes_type(self):
        payload = json.dumps(
            {"id": "evt_1", "type": "charge.succeeded", "data": {"object": {"id": "ch_123"}}}
        ).encode()
        event = self.provider.parse_webhook_event(payload=payload)
        self.assertEqual(event.event_type, "payment.succeeded")
        self.assertEqual(event.provider_reference, "ch_123")
        self.assertEqual(event.event_id, "evt_1")


class ChapaWebhookSignatureTests(TestCase):
    def setUp(self):
        with override_settings(CHAPA_SECRET_KEY="sk_test_x", CHAPA_WEBHOOK_SECRET="chapa_whsec"):
            self.provider = ChapaProvider()

    def test_valid_signature_is_accepted(self):
        payload = json.dumps({"tx_ref": "tx_1", "status": "success"}).encode()
        signature = hmac.new(b"chapa_whsec", payload, hashlib.sha256).hexdigest()
        self.assertTrue(self.provider.verify_webhook_signature(payload=payload, signature_header=signature))

    def test_tampered_payload_is_rejected(self):
        payload = json.dumps({"tx_ref": "tx_1", "status": "success"}).encode()
        signature = hmac.new(b"chapa_whsec", payload, hashlib.sha256).hexdigest()
        tampered = json.dumps({"tx_ref": "tx_1", "status": "failed"}).encode()
        self.assertFalse(self.provider.verify_webhook_signature(payload=tampered, signature_header=signature))

    def test_parse_webhook_event_derives_stable_event_id(self):
        payload = json.dumps({"tx_ref": "tx_1", "status": "success"}).encode()
        event = self.provider.parse_webhook_event(payload=payload)
        self.assertEqual(event.event_id, "tx_1:success")
        self.assertEqual(event.event_type, "payment.succeeded")


class StripeChargeAndRefundTests(TestCase):
    """Provider REST calls mocked — this sandbox can't reach Stripe's API."""

    def setUp(self):
        with override_settings(STRIPE_SECRET_KEY="sk_test_x", STRIPE_WEBHOOK_SECRET="whsec_test"):
            self.provider = StripeProvider()

    @mock.patch("apps.payments.providers.stripe_provider.requests.post")
    def test_charge_requires_a_payment_token(self, mock_post):
        with self.assertRaises(PaymentProviderError):
            self.provider.charge(amount=Decimal("5.50"), currency="usd", idempotency_key="idem1")
        mock_post.assert_not_called()

    @mock.patch("apps.payments.providers.stripe_provider.requests.post")
    def test_successful_charge_parses_response(self, mock_post):
        mock_post.return_value = mock.Mock(status_code=200, json=lambda: {"id": "ch_abc", "status": "succeeded"})
        result = self.provider.charge(
            amount=Decimal("5.50"), currency="usd", idempotency_key="idem1", payment_token="tok_visa"
        )
        self.assertEqual(result.provider_reference, "ch_abc")
        self.assertEqual(result.status, "succeeded")
        call_kwargs = mock_post.call_args.kwargs
        self.assertEqual(call_kwargs["data"]["amount"], 550)  # cents

    @mock.patch("apps.payments.providers.stripe_provider.requests.post")
    def test_declined_charge_raises(self, mock_post):
        mock_post.return_value = mock.Mock(status_code=402, json=lambda: {"error": {"message": "Card declined"}})
        with self.assertRaises(PaymentProviderError):
            self.provider.charge(amount=Decimal("5.50"), currency="usd", idempotency_key="idem1", payment_token="tok_bad")


class PaymentInitiationTests(TestCase):
    def test_cash_payment_stays_pending_until_confirmed(self):
        ride, rider, driver = _completed_ride_with_final_fare()
        payment = initiate_payment(ride, method=PaymentMethod.CASH)
        self.assertEqual(payment.status, PaymentStatus.PENDING)
        self.assertEqual(payment.amount, ride.fares.get(fare_type="FINAL").total_amount)

    def test_initiating_twice_returns_the_same_payment(self):
        ride, rider, driver = _completed_ride_with_final_fare()
        first = initiate_payment(ride, method=PaymentMethod.CASH)
        second = initiate_payment(ride, method=PaymentMethod.CASH)
        self.assertEqual(first.id, second.id)
        self.assertEqual(Payment.objects.filter(ride=ride).count(), 1)

    def test_non_cash_requires_a_provider(self):
        ride, rider, driver = _completed_ride_with_final_fare()
        with self.assertRaises(PaymentError):
            initiate_payment(ride, method=PaymentMethod.CARD)

    def test_cannot_initiate_before_ride_reaches_payment_pending(self):
        rider = User.objects.create(email="early-pay@example.com", keycloak_id="kc-early-pay")
        ride = create_ride(rider=rider, **_ride_kwargs())  # still SEARCHING_DRIVER
        with self.assertRaises(PaymentError):
            initiate_payment(ride, method=PaymentMethod.CASH)

    @mock.patch("apps.payments.services.get_payment_provider")
    def test_successful_card_charge_marks_ride_paid(self, mock_get_provider):
        from apps.payments.providers.base import ChargeResult

        mock_provider = mock.Mock()
        mock_provider.charge.return_value = ChargeResult(provider_reference="ch_1", status="succeeded", raw_response={})
        mock_get_provider.return_value = mock_provider

        ride, rider, driver = _completed_ride_with_final_fare()
        payment = initiate_payment(ride, method=PaymentMethod.CARD, provider_name="stripe", payment_token="tok_visa")

        self.assertEqual(payment.status, PaymentStatus.CAPTURED)
        ride.refresh_from_db()
        self.assertEqual(ride.status, RideStatus.PAID)

    @mock.patch("apps.payments.services.get_payment_provider")
    def test_failed_charge_marks_payment_failed_without_marking_ride_paid(self, mock_get_provider):
        mock_provider = mock.Mock()
        mock_provider.charge.side_effect = PaymentProviderError("Card declined")
        mock_get_provider.return_value = mock_provider

        ride, rider, driver = _completed_ride_with_final_fare()
        with self.assertRaises(PaymentError):
            initiate_payment(ride, method=PaymentMethod.CARD, provider_name="stripe", payment_token="tok_bad")

        payment = Payment.objects.get(ride=ride)
        self.assertEqual(payment.status, PaymentStatus.FAILED)
        ride.refresh_from_db()
        self.assertEqual(ride.status, RideStatus.PAYMENT_PENDING)


class CashConfirmationTests(TestCase):
    def test_driver_confirming_cash_marks_ride_paid(self):
        ride, rider, driver = _completed_ride_with_final_fare()
        initiate_payment(ride, method=PaymentMethod.CASH)

        payment = confirm_cash_payment(ride, driver)
        self.assertEqual(payment.status, PaymentStatus.CAPTURED)
        ride.refresh_from_db()
        self.assertEqual(ride.status, RideStatus.PAID)

    def test_confirming_twice_is_idempotent_not_an_error(self):
        ride, rider, driver = _completed_ride_with_final_fare()
        initiate_payment(ride, method=PaymentMethod.CASH)
        confirm_cash_payment(ride, driver)
        confirm_cash_payment(ride, driver)  # should not raise
        count = PaymentTransaction.objects.filter(
            payment__ride=ride, transaction_type=PaymentTransactionType.CAPTURE
        ).count()
        self.assertEqual(count, 1)

    def test_unassigned_driver_cannot_confirm(self):
        ride, rider, driver = _completed_ride_with_final_fare()
        initiate_payment(ride, method=PaymentMethod.CASH)

        other_user = User.objects.create(email="other-cash-driver@example.com", keycloak_id="kc-other-cash-driver")
        admin = User.objects.create(email="other-cash-admin@example.com", keycloak_id="kc-other-cash-admin")
        other_driver = apply_as_driver(other_user, license_number="D-OTHER", license_expiry="2030-01-01")
        approve_driver(other_driver, admin)

        with self.assertRaises(PaymentError):
            confirm_cash_payment(ride, other_driver)


class WebhookIdempotencyTests(TestCase):
    """The core Phase 8 guarantee: duplicate webhook delivery never double-charges or double-records."""

    def setUp(self):
        clear_provider_cache()
        self.stripe_settings = override_settings(STRIPE_SECRET_KEY="sk_test_x", STRIPE_WEBHOOK_SECRET="whsec_test")
        self.stripe_settings.enable()
        self.addCleanup(self.stripe_settings.disable)
        self.addCleanup(clear_provider_cache)

        self.ride, self.rider, self.driver = _completed_ride_with_final_fare()
        fare = self.ride.fares.get(fare_type="FINAL")
        # Simulate an AUTHORIZED-but-not-yet-captured card payment (e.g. a
        # provider that confirms asynchronously) by constructing the
        # Payment directly, rather than exercising the real charge() call
        # this test isn't exercising — that call requires a payment_token
        # and would otherwise fail before we ever reach webhook handling.
        self.payment = Payment.objects.create(
            ride=self.ride,
            fare=fare,
            method=PaymentMethod.CARD,
            provider_name="stripe",
            currency=fare.currency,
            amount=fare.total_amount,
            provider_reference="ch_webhook_test",
            status=PaymentStatus.AUTHORIZED,
            idempotency_key="test-webhook-idem-key",
        )

    def _webhook_payload_and_signature(self, event_id="evt_1", event_type="charge.succeeded"):
        payload = json.dumps(
            {"id": event_id, "type": event_type, "data": {"object": {"id": "ch_webhook_test"}}}
        ).encode()
        signed_payload = f"1700000000.{payload.decode()}".encode()
        signature = hmac.new(b"whsec_test", signed_payload, hashlib.sha256).hexdigest()
        return payload, f"t=1700000000,v1={signature}"

    def test_first_delivery_captures_the_payment(self):
        payload, header = self._webhook_payload_and_signature()
        processed = process_webhook("stripe", raw_body=payload, signature_header=header)

        self.assertTrue(processed)
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, PaymentStatus.CAPTURED)
        self.ride.refresh_from_db()
        self.assertEqual(self.ride.status, RideStatus.PAID)

    def test_duplicate_delivery_of_the_same_event_is_a_no_op(self):
        payload, header = self._webhook_payload_and_signature()
        process_webhook("stripe", raw_body=payload, signature_header=header)

        second_result = process_webhook("stripe", raw_body=payload, signature_header=header)

        self.assertFalse(second_result)
        count = PaymentTransaction.objects.filter(
            payment=self.payment, transaction_type=PaymentTransactionType.CAPTURE
        ).count()
        self.assertEqual(count, 1)

    def test_duplicate_delivery_does_not_re_trigger_ride_paid_transition(self):
        payload, header = self._webhook_payload_and_signature()
        process_webhook("stripe", raw_body=payload, signature_header=header)
        process_webhook("stripe", raw_body=payload, signature_header=header)

        # mark_paid is itself idempotent too (see apps/rides/services.py),
        # so this should simply still be PAID, not raise or duplicate history.
        self.ride.refresh_from_db()
        self.assertEqual(self.ride.status, RideStatus.PAID)

    def test_invalid_signature_is_rejected_before_any_processing(self):
        from apps.payments.providers.base import InvalidWebhookSignature

        payload, _ = self._webhook_payload_and_signature()
        with self.assertRaises(InvalidWebhookSignature):
            process_webhook("stripe", raw_body=payload, signature_header="t=123,v1=deadbeef")

        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, PaymentStatus.AUTHORIZED)  # unchanged

    def test_failed_event_marks_payment_failed(self):
        payload, header = self._webhook_payload_and_signature(event_id="evt_2", event_type="charge.failed")
        process_webhook("stripe", raw_body=payload, signature_header=header)

        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, PaymentStatus.FAILED)


class RefundTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create(email="refund-admin@example.com", keycloak_id="kc-refund-admin")

    def test_full_refund_of_cash_payment_is_recorded(self):
        ride, rider, driver = _completed_ride_with_final_fare()
        payment = initiate_payment(ride, method=PaymentMethod.CASH)
        payment = confirm_cash_payment(ride, driver)

        refund_payment(payment, admin_user=self.admin, reason="Rider complaint")
        payment.refresh_from_db()
        self.assertEqual(payment.status, PaymentStatus.REFUNDED)

    def test_partial_refund_sets_partially_refunded_status(self):
        ride, rider, driver = _completed_ride_with_final_fare()
        payment = initiate_payment(ride, method=PaymentMethod.CASH)
        payment = confirm_cash_payment(ride, driver)

        refund_payment(payment, admin_user=self.admin, amount=Decimal("1.00"), reason="Goodwill credit")
        payment.refresh_from_db()
        self.assertEqual(payment.status, PaymentStatus.PARTIALLY_REFUNDED)

    def test_cannot_refund_more_than_the_original_amount(self):
        ride, rider, driver = _completed_ride_with_final_fare()
        payment = initiate_payment(ride, method=PaymentMethod.CASH)
        payment = confirm_cash_payment(ride, driver)

        with self.assertRaises(PaymentError):
            refund_payment(payment, admin_user=self.admin, amount=Decimal("999.00"), reason="oops")

    def test_cannot_refund_a_pending_payment(self):
        ride, rider, driver = _completed_ride_with_final_fare()
        payment = initiate_payment(ride, method=PaymentMethod.CASH)  # never confirmed
        with self.assertRaises(PaymentError):
            refund_payment(payment, admin_user=self.admin, reason="too early")


class PaymentApiTests(APITestCase):
    def setUp(self):
        self.ops_admin = User.objects.create(email="pay-api-admin@example.com", keycloak_id="kc-pay-api-admin")
        assign_role(self.ops_admin, "Finance Admin")

    def test_rider_can_initiate_cash_payment(self):
        ride, rider, driver = _completed_ride_with_final_fare()
        self.client.force_authenticate(user=rider)

        response = self.client.post(reverse("ride_payment", args=[ride.id]), {"method": "CASH"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["status"], "PENDING")

    def test_non_rider_cannot_initiate_payment(self):
        ride, rider, driver = _completed_ride_with_final_fare()
        other = User.objects.create(email="not-the-rider@example.com", keycloak_id="kc-not-the-rider")
        self.client.force_authenticate(user=other)

        response = self.client.post(reverse("ride_payment", args=[ride.id]), {"method": "CASH"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_driver_confirms_cash_over_the_api(self):
        ride, rider, driver = _completed_ride_with_final_fare()
        self.client.force_authenticate(user=rider)
        self.client.post(reverse("ride_payment", args=[ride.id]), {"method": "CASH"}, format="json")

        self.client.force_authenticate(user=driver.user)
        response = self.client.post(reverse("confirm_cash_payment", args=[ride.id]))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["status"], "CAPTURED")

    def test_guest_can_initiate_and_view_payment_via_tracking_token(self):
        sedan = VehicleType.objects.get(name="Sedan")
        driver_user = User.objects.create(email="guest-pay-driver@example.com", keycloak_id="kc-guest-pay-driver")
        admin = User.objects.create(email="guest-pay-admin@example.com", keycloak_id="kc-guest-pay-admin")
        driver = apply_as_driver(driver_user, license_number="D-GP", license_expiry="2030-01-01")
        approve_driver(driver, admin)
        register_vehicle(driver, vehicle_type=sedan, make="Toyota", model="Corolla", year=2022, license_plate="GUEST-PAY-1")

        ride = create_ride(guest_phone_number="+15557778888", **_ride_kwargs())
        ride.estimated_distance_km = Decimal("5.0")
        ride.estimated_duration_minutes = Decimal("10.0")
        ride.save()
        estimate_fare_for_ride(ride)
        create_offers(ride, [(driver, 0.2)])
        accept_ride(ride.id, driver)
        mark_arriving(ride.id, driver)
        mark_arrived(ride.id, driver)
        start_trip(ride.id, driver)
        complete_trip(ride.id, driver)

        response = self.client.post(
            reverse("guest_ride_payment", args=[ride.tracking_token]), {"method": "CASH"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        get_response = self.client.get(reverse("guest_ride_payment", args=[ride.tracking_token]))
        self.assertEqual(get_response.status_code, status.HTTP_200_OK)

    def test_finance_admin_can_refund(self):
        ride, rider, driver = _completed_ride_with_final_fare()
        self.client.force_authenticate(user=rider)
        self.client.post(reverse("ride_payment", args=[ride.id]), {"method": "CASH"}, format="json")
        self.client.force_authenticate(user=driver.user)
        self.client.post(reverse("confirm_cash_payment", args=[ride.id]))

        payment = Payment.objects.get(ride=ride)
        self.client.force_authenticate(user=self.ops_admin)
        response = self.client.post(
            reverse("admin_payment_refund", args=[payment.id]), {"reason": "Rider complaint"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["status"], "REFUNDED")

    def test_rider_cannot_refund(self):
        ride, rider, driver = _completed_ride_with_final_fare()
        payment = initiate_payment(ride, method=PaymentMethod.CASH)

        self.client.force_authenticate(user=rider)
        response = self.client.post(
            reverse("admin_payment_refund", args=[payment.id]), {"reason": "trying my luck"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @override_settings(STRIPE_SECRET_KEY="sk_test_x", STRIPE_WEBHOOK_SECRET="whsec_test")
    def test_webhook_endpoint_rejects_bad_signature(self):
        clear_provider_cache()
        self.addCleanup(clear_provider_cache)
        payload = json.dumps({"id": "evt_x", "type": "charge.succeeded"}).encode()
        response = self.client.post(
            reverse("payment_webhook", args=["stripe"]),
            data=payload,
            content_type="application/json",
            HTTP_STRIPE_SIGNATURE="t=123,v1=deadbeef",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_webhook_endpoint_for_unknown_provider_is_rejected_cleanly(self):
        response = self.client.post(
            reverse("payment_webhook", args=["not_a_real_provider"]),
            data=b"{}",
            content_type="application/json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
