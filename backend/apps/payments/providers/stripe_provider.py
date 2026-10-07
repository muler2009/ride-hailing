"""
Real HTTP calls to Stripe's REST API (Charges + Refunds), and Stripe's
documented webhook signature scheme implemented directly rather than via
their SDK — the same "thin wrapper over plain requests" style used for
Keycloak (Phase 1) and the map providers (Phase 6).

NOTE ON TESTABILITY: this sandbox's network can't reach Stripe's API, so
apps/payments/tests.py mocks the `requests` calls for charge/refund. The
webhook *signature verification*, however, is pure HMAC computation with
no network involved — tests compute a real signature with a test secret
and confirm verify_webhook_signature() genuinely validates it (and
rejects a tampered payload), the same approach used for Keycloak's
self-signed JWTs in Phase 1.

Uses the legacy Charges API (not PaymentIntents) deliberately: this
platform charges once, at trip completion, with no pre-authorization step
(see Payment's docstring) — a single combined charge is all that's needed,
and PaymentIntents' multi-step confirmation flow would add complexity this
use case doesn't call for.
"""
import hashlib
import hmac
import json
from decimal import Decimal

import requests
from django.conf import settings

from apps.payments.providers.base import (
    ChargeResult,
    PaymentProvider,
    PaymentProviderError,
    WebhookEvent,
)

REQUEST_TIMEOUT_SECONDS = 15
API_BASE_URL = "https://api.stripe.com/v1"

_EVENT_TYPE_MAP = {
    "charge.succeeded": "payment.succeeded",
    "charge.failed": "payment.failed",
    "charge.refunded": "refund.succeeded",
}


class StripeProvider(PaymentProvider):
    def __init__(self):
        self.secret_key = settings.STRIPE_SECRET_KEY
        self.webhook_secret = settings.STRIPE_WEBHOOK_SECRET
        if not self.secret_key:
            raise PaymentProviderError("STRIPE_SECRET_KEY is not configured.")

    def _headers(self, idempotency_key: str) -> dict:
        return {"Idempotency-Key": idempotency_key}

    def _auth(self):
        return (self.secret_key, "")

    def charge(self, *, amount: Decimal, currency: str, idempotency_key: str, payment_token: str = None, description: str = "") -> ChargeResult:
        if not payment_token:
            raise PaymentProviderError("A payment_token (Stripe source/token) is required to charge a card.")

        response = requests.post(
            f"{API_BASE_URL}/charges",
            auth=self._auth(),
            headers=self._headers(idempotency_key),
            data={
                "amount": int(amount * 100),  # Stripe amounts are in the smallest currency unit
                "currency": currency.lower(),
                "source": payment_token,
                "description": description,
            },
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        data = response.json()
        if response.status_code != 200:
            raise PaymentProviderError(f"Stripe charge failed: {data.get('error', {}).get('message', response.text)}")

        return ChargeResult(provider_reference=data["id"], status=data["status"], raw_response=data)

    def refund(self, *, provider_reference: str, amount: Decimal, idempotency_key: str) -> ChargeResult:
        response = requests.post(
            f"{API_BASE_URL}/refunds",
            auth=self._auth(),
            headers=self._headers(idempotency_key),
            data={"charge": provider_reference, "amount": int(amount * 100)},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        data = response.json()
        if response.status_code != 200:
            raise PaymentProviderError(f"Stripe refund failed: {data.get('error', {}).get('message', response.text)}")

        return ChargeResult(provider_reference=data["id"], status=data["status"], raw_response=data)

    def verify_webhook_signature(self, *, payload: bytes, signature_header: str) -> bool:
        """
        Implements Stripe's documented scheme exactly: the header is
        `t=<timestamp>,v1=<hex hmac>`; the signed payload is
        `"{timestamp}.{raw body}"`, HMAC-SHA256'd with the webhook secret.
        """
        try:
            parts = dict(p.split("=", 1) for p in signature_header.split(","))
            timestamp, signature = parts["t"], parts["v1"]
        except (KeyError, ValueError):
            return False

        signed_payload = f"{timestamp}.{payload.decode('utf-8')}".encode("utf-8")
        expected = hmac.new(self.webhook_secret.encode("utf-8"), signed_payload, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, signature)

    def parse_webhook_event(self, *, payload: bytes) -> WebhookEvent:
        data = json.loads(payload)
        stripe_type = data.get("type", "")
        event_type = _EVENT_TYPE_MAP.get(stripe_type, stripe_type)
        charge_object = data.get("data", {}).get("object", {})
        return WebhookEvent(
            event_id=data["id"],
            provider_reference=charge_object.get("id", ""),
            event_type=event_type,
            raw_response=data,
        )
