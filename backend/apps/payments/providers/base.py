"""
The abstraction from the architecture doc's Payments section:

    PaymentProvider
    ├── StripeProvider
    ├── ChapaProvider
    └── TelebirrProvider (not implemented — Telebirr's proprietary
        RSA-signed request protocol is significantly more involved than a
        standard REST+HMAC integration; adding it means a new
        providers/telebirr.py implementing this same interface, not a
        change anywhere else)

No calling code should import a specific provider directly — always go
through apps/payments/factory.py:get_payment_provider(name). CASH never
goes through a provider at all (see apps/payments/services.py) since
there's no external gateway involved.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional


class PaymentProviderError(Exception):
    """Raised for any failure talking to a payment provider — network, auth, or a declined charge."""


class InvalidWebhookSignature(PaymentProviderError):
    """Raised when a webhook's signature doesn't verify — never process an unverified webhook body."""


@dataclass(frozen=True)
class ChargeResult:
    provider_reference: str
    status: str  # provider-native status string, not one of our PaymentStatus values
    raw_response: dict


@dataclass(frozen=True)
class WebhookEvent:
    event_id: str  # the provider's own id for this event — the idempotency key's raw material
    provider_reference: str  # which charge this event is about
    event_type: str  # normalized: "payment.succeeded" | "payment.failed" | "refund.succeeded"
    raw_response: dict


class PaymentProvider(ABC):
    @abstractmethod
    def charge(
        self, *, amount: Decimal, currency: str, idempotency_key: str,
        payment_token: Optional[str] = None, description: str = "",
    ) -> ChargeResult:
        """
        Creates and captures a charge in one step. Real card networks
        separate authorize/capture, but since this platform only charges
        at trip completion (no pre-authorization — see Payment's
        docstring), a single combined operation is all that's needed here.
        `idempotency_key` must be passed through to the provider's own
        idempotency mechanism where one exists (Stripe supports this
        natively via a request header). `payment_token` is a client-side
        tokenized payment method (e.g. from Stripe.js) where the provider
        needs one up front; hosted-checkout providers like Chapa ignore it
        and instead return a URL for the client to redirect to.
        """

    @abstractmethod
    def refund(self, *, provider_reference: str, amount: Decimal, idempotency_key: str) -> ChargeResult:
        """Refunds all or part of a previously captured charge."""

    @abstractmethod
    def verify_webhook_signature(self, *, payload: bytes, signature_header: str) -> bool:
        """Returns True only if payload genuinely originated from this provider."""

    @abstractmethod
    def parse_webhook_event(self, *, payload: bytes) -> WebhookEvent:
        """
        Parses an already-signature-verified payload into our normalized
        WebhookEvent shape. Never call this on an unverified payload.
        """
