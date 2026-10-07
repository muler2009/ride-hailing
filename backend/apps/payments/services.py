"""
Payment status is NEVER set from anything a client asserts. It's set from
exactly three places, all server-controlled:
  1. a provider webhook whose signature has been verified
  2. a driver explicitly confirming cash received (an authorized action,
     not a claim about payment state)
  3. an admin-initiated refund

Idempotency has two distinct guarantees here:
  - Payment INITIATION is idempotent: calling initiate_payment() twice for
    the same ride returns the existing Payment rather than creating a
    second one (Payment.ride is OneToOne).
  - Webhook PROCESSING is idempotent against duplicate delivery — not via
    a check-then-act read, which races, but via a unique constraint on
    PaymentTransaction.idempotency_key that the database itself enforces.
    A retried webhook hits that constraint and is treated as an
    already-processed no-op, exactly the "duplicate webhook" race the
    architecture doc calls out explicitly.
"""
import uuid
from decimal import Decimal

from django.db import IntegrityError, transaction

from apps.payments.factory import get_payment_provider
from apps.payments.models import Payment, PaymentMethod, PaymentStatus, PaymentTransaction, PaymentTransactionType
from apps.payments.providers.base import InvalidWebhookSignature, PaymentProviderError


class PaymentError(Exception):
    pass


def _generate_idempotency_key() -> str:
    return uuid.uuid4().hex


def initiate_payment(ride, *, method: str, provider_name: str = "", payment_token: str = None) -> Payment:
    """
    Idempotent: if this ride already has a Payment, it's returned as-is —
    callers should check `payment.status` rather than assume a fresh
    PENDING record.

    Deliberately NOT one single @transaction.atomic block end to end: if
    the provider charge fails, the Payment record itself (and its FAILED
    status) must still be persisted as an audit trail — wrapping
    everything in one transaction would roll that back right along with
    the failed charge attempt when the error propagates to the caller.
    Only each individual write gets its own atomic boundary.
    """
    existing = Payment.objects.filter(ride=ride).first()
    if existing is not None:
        return existing

    from apps.rides.models import RideStatus

    if ride.status != RideStatus.PAYMENT_PENDING:
        raise PaymentError(f"Ride is not awaiting payment (current status: {ride.status}).")

    fare = ride.fares.filter(fare_type="FINAL").first()
    if fare is None:
        raise PaymentError("Ride has no final fare to charge yet.")

    if method != PaymentMethod.CASH and not provider_name:
        raise PaymentError(f"A provider is required for payment method {method}.")

    with transaction.atomic():
        payment = Payment.objects.create(
            ride=ride,
            fare=fare,
            method=method,
            provider_name="" if method == PaymentMethod.CASH else provider_name,
            currency=fare.currency,
            amount=fare.total_amount,
            idempotency_key=_generate_idempotency_key(),
        )

    if method == PaymentMethod.CASH:
        return payment  # stays PENDING until the driver confirms cash received

    try:
        provider = get_payment_provider(provider_name)
        result = provider.charge(
            amount=payment.amount,
            currency=payment.currency,
            idempotency_key=payment.idempotency_key,
            payment_token=payment_token,
            description=f"Ride {ride.id}",
        )
    except PaymentProviderError as exc:
        payment.status = PaymentStatus.FAILED
        payment.failure_reason = str(exc)
        payment.save(update_fields=["status", "failure_reason"])
        _notify_payment_event(ride, "PAYMENT_FAILED", "Payment failed", "We couldn't process your payment — please try another method.")
        raise PaymentError(str(exc)) from exc

    with transaction.atomic():
        payment.provider_reference = result.provider_reference
        payment.status = PaymentStatus.CAPTURED if result.status in ("succeeded", "paid") else PaymentStatus.AUTHORIZED
        payment.save(update_fields=["provider_reference", "status"])

        PaymentTransaction.objects.create(
            payment=payment,
            transaction_type=PaymentTransactionType.AUTHORIZATION,
            amount=payment.amount,
            provider_reference=result.provider_reference,
            idempotency_key=f"init:{payment.idempotency_key}",
            raw_response=result.raw_response,
        )

    if payment.status == PaymentStatus.CAPTURED:
        _mark_ride_paid(ride)

    return payment


def confirm_cash_payment(ride, driver) -> Payment:
    """Driver-initiated — the only way a CASH payment ever becomes CAPTURED."""
    payment = Payment.objects.filter(ride=ride).first()
    if payment is None:
        raise PaymentError("No payment has been initiated for this ride yet.")
    if payment.method != PaymentMethod.CASH:
        raise PaymentError("This ride's payment method is not cash.")
    if ride.driver_id != driver.id:
        raise PaymentError("You are not the driver assigned to this ride.")
    if payment.status == PaymentStatus.CAPTURED:
        return payment  # idempotent: confirming twice is a no-op, not an error

    with transaction.atomic():
        payment.status = PaymentStatus.CAPTURED
        payment.save(update_fields=["status"])
        PaymentTransaction.objects.create(
            payment=payment,
            transaction_type=PaymentTransactionType.CAPTURE,
            amount=payment.amount,
            idempotency_key=f"cash:{payment.id}",
            initiated_by=driver.user,
        )
        _mark_ride_paid(payment.ride)

    return payment


def process_webhook(provider_name: str, *, raw_body: bytes, signature_header: str) -> bool:
    """
    Returns True if this delivery was newly processed, False if it was a
    duplicate (already-processed) delivery — both are a successful
    outcome from the provider's point of view (respond 200 either way),
    but the distinction is useful for logging/monitoring.
    """

    provider = get_payment_provider(provider_name)

    if not provider.verify_webhook_signature(payload=raw_body, signature_header=signature_header):
        raise InvalidWebhookSignature("Webhook signature verification failed.")

    event = provider.parse_webhook_event(payload=raw_body)

    payment = Payment.objects.filter(provider_reference=event.provider_reference).first()
    if payment is None:
        raise PaymentError(f"No payment found for provider reference {event.provider_reference!r}.")

    transaction_type = {
        "payment.succeeded": PaymentTransactionType.CAPTURE,
        "payment.failed": PaymentTransactionType.FAILURE,
        "refund.succeeded": PaymentTransactionType.REFUND,
    }.get(event.event_type, PaymentTransactionType.FAILURE)

    try:
        with transaction.atomic():
            PaymentTransaction.objects.create(
                payment=payment,
                transaction_type=transaction_type,
                amount=payment.amount,
                provider_reference=event.provider_reference,
                idempotency_key=f"webhook:{provider_name}:{event.event_id}",
                raw_response=event.raw_response,
            )

            if event.event_type == "payment.succeeded":
                payment.status = PaymentStatus.CAPTURED
                payment.save(update_fields=["status"])
                _mark_ride_paid(payment.ride)
            elif event.event_type == "payment.failed":
                payment.status = PaymentStatus.FAILED
                payment.save(update_fields=["status"])
                _notify_payment_event(payment.ride, "PAYMENT_FAILED", "Payment failed", "We couldn't process your payment — please try another method.")
    except IntegrityError:
        # The unique constraint on idempotency_key is what actually
        # guarantees this — not the check above, which would itself race
        # under concurrent delivery. A duplicate delivery lands here and
        # is treated as already handled.
        return False

    return True


def refund_payment(payment: Payment, *, admin_user, amount: Decimal = None, reason: str = "") -> PaymentTransaction:
    if payment.status not in (PaymentStatus.CAPTURED, PaymentStatus.PARTIALLY_REFUNDED):
        raise PaymentError(f"Cannot refund a payment in status {payment.status}.")

    refund_amount = amount if amount is not None else payment.amount
    if refund_amount <= 0 or refund_amount > payment.amount:
        raise PaymentError("Refund amount must be positive and no greater than the original payment.")

    idempotency_key = f"refund:{payment.id}:{admin_user.id}:{refund_amount}"

    with transaction.atomic():
        if payment.method != PaymentMethod.CASH:
            provider = get_payment_provider(payment.provider_name)
            try:
                result = provider.refund(
                    provider_reference=payment.provider_reference,
                    amount=refund_amount,
                    idempotency_key=idempotency_key,
                )
                raw_response = result.raw_response
            except PaymentProviderError as exc:
                raise PaymentError(str(exc)) from exc
        else:
            # Cash can't be refunded through a gateway — this records an
            # administrative refund decision (e.g. a goodwill credit
            # issued some other way) with the same audit trail, not an
            # actual reversal of physical cash already handed over.
            raw_response = {"note": "Cash payment — administrative refund record only.", "reason": reason}

        try:
            txn = PaymentTransaction.objects.create(
                payment=payment,
                transaction_type=PaymentTransactionType.REFUND,
                amount=refund_amount,
                provider_reference=payment.provider_reference,
                idempotency_key=idempotency_key,
                raw_response=raw_response,
                initiated_by=admin_user,
            )
        except IntegrityError:
            raise PaymentError("This exact refund has already been processed.")

        payment.status = (
            PaymentStatus.REFUNDED if refund_amount == payment.amount else PaymentStatus.PARTIALLY_REFUNDED
        )
        payment.save(update_fields=["status"])

    from apps.admin_api.audit import record_audit

    record_audit(
        actor=admin_user, action="PAYMENT_REFUNDED", target_type="Payment", target_id=payment.id,
        reason=reason, changes={"amount": str(refund_amount), "new_status": payment.status},
    )

    return txn


def _mark_ride_paid(ride) -> None:
    from apps.rides.services import mark_paid

    mark_paid(ride.id)
    _notify_payment_event(ride, "PAYMENT_COMPLETED", "Payment received", "Your payment for this trip was successful.")


def _notify_payment_event(ride, event_type: str, title: str, body: str) -> None:
    """
    Best-effort, same tolerance pattern used throughout this codebase for
    cross-context calls — a notification-dispatch hiccup should never
    unwind a payment outcome that has already genuinely happened.
    """
    try:
        from apps.notifications.services import notify

        notify(
            event_type=event_type,
            title=title,
            body=body,
            recipient_user=ride.rider,
            guest_phone_number=ride.guest_phone_number or "",
            ride=ride,
        )
    except Exception:
        import logging

        logging.getLogger(__name__).exception(
            "Notification dispatch failed for ride %s event %s", ride.id, event_type
        )
