"""
Real HTTP calls to Chapa's REST API (https://developer.chapa.co) — an
Ethiopian payment gateway supporting cards and local mobile money,
explicitly named in the architecture doc's PaymentProvider tree.

Structurally different from Stripe on purpose, to prove the abstraction
holds across genuinely different provider models: Chapa is a
hosted-checkout gateway — charge() initializes a transaction and returns
a checkout_url for the client to redirect to, rather than completing the
charge synchronously. The actual outcome arrives later via webhook.

Chapa identifies a transaction by a merchant-supplied `tx_ref` rather than
issuing its own opaque charge id up front, so this provider uses our own
idempotency_key AS the tx_ref — one merchant-controlled value serves both
purposes, which is exactly what tx_ref is for.

Same testability note as stripe_provider.py: `requests` calls are mocked
in tests; webhook signature verification is pure HMAC computation, tested
for real. Chapa's webhook payload doesn't include a distinct per-delivery
event id the way Stripe's does, so event_id is derived from
`{tx_ref}:{status}` — stable across genuine retries of the same
notification (correct idempotency), and distinct if Chapa later sends a
different status for the same tx_ref (e.g. a later refund event).
"""
import hashlib
import hmac
import json
from decimal import Decimal

import requests
from django.conf import settings

from apps.payments.providers.base import ChargeResult, PaymentProvider, PaymentProviderError, WebhookEvent

REQUEST_TIMEOUT_SECONDS = 15
API_BASE_URL = "https://api.chapa.co/v1"


class ChapaProvider(PaymentProvider):
    def __init__(self):
        self.secret_key = settings.CHAPA_SECRET_KEY
        self.webhook_secret = settings.CHAPA_WEBHOOK_SECRET
        self.callback_url = settings.CHAPA_CALLBACK_URL
        if not self.secret_key:
            raise PaymentProviderError("CHAPA_SECRET_KEY is not configured.")

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.secret_key}"}

    def charge(self, *, amount: Decimal, currency: str, idempotency_key: str, payment_token: str = None, description: str = "") -> ChargeResult:
        response = requests.post(
            f"{API_BASE_URL}/transaction/initialize",
            headers=self._headers(),
            json={
                "amount": str(amount),
                "currency": currency,
                "tx_ref": idempotency_key,
                "callback_url": self.callback_url,
                "customization": {"title": "Ride payment", "description": description or "Ride payment"},
            },
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        data = response.json()
        if response.status_code != 200 or data.get("status") != "success":
            raise PaymentProviderError(f"Chapa initialization failed: {data.get('message', response.text)}")

        # No charge has actually happened yet — the rider still has to
        # complete the hosted checkout. "pending" reflects that; our
        # webhook handler is what eventually moves this to captured/failed.
        return ChargeResult(provider_reference=idempotency_key, status="pending", raw_response=data)

    def refund(self, *, provider_reference: str, amount: Decimal, idempotency_key: str) -> ChargeResult:
        response = requests.post(
            f"{API_BASE_URL}/refund/{provider_reference}",
            headers=self._headers(),
            json={"amount": str(amount), "reason": "Refund requested"},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        data = response.json()
        if response.status_code != 200 or data.get("status") != "success":
            raise PaymentProviderError(f"Chapa refund failed: {data.get('message', response.text)}")

        return ChargeResult(provider_reference=provider_reference, status="success", raw_response=data)

    def verify_webhook_signature(self, *, payload: bytes, signature_header: str) -> bool:
        expected = hmac.new(self.webhook_secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, signature_header)

    def parse_webhook_event(self, *, payload: bytes) -> WebhookEvent:
        data = json.loads(payload)
        tx_ref = data.get("tx_ref", "")
        chapa_status = data.get("status", "")
        event_type = {"success": "payment.succeeded", "failed": "payment.failed"}.get(chapa_status, chapa_status)
        return WebhookEvent(
            event_id=f"{tx_ref}:{chapa_status}",
            provider_reference=tx_ref,
            event_type=event_type,
            raw_response=data,
        )
