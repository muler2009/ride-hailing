from django.conf import settings
from django.db import models

from apps.common.models import BaseModel


class PaymentMethod(models.TextChoices):
    CASH = "CASH", "Cash"
    CARD = "CARD", "Card"
    MOBILE_MONEY = "MOBILE_MONEY", "Mobile Money"
    # WALLET is intentionally not offered yet — the Wallet model doesn't
    # exist until Phase 9. Adding it here without a real balance to debit
    # would be a payment method nothing can actually fulfill.


class PaymentStatus(models.TextChoices):
    PENDING = "PENDING", "Pending"
    AUTHORIZED = "AUTHORIZED", "Authorized"
    CAPTURED = "CAPTURED", "Captured"
    FAILED = "FAILED", "Failed"
    REFUNDED = "REFUNDED", "Refunded"
    PARTIALLY_REFUNDED = "PARTIALLY_REFUNDED", "Partially Refunded"


class Payment(BaseModel):
    """
    One payment per ride, tied to that ride's FINAL fare (see Phase 7).
    Only reachable once the ride is PAYMENT_PENDING — this platform
    doesn't pre-authorize a card at request time, only charge at
    completion, which keeps the flow considerably simpler at the cost of
    not catching a bad card until the trip is already over. A production
    system might reasonably want pre-authorization; that's a deliberate
    scope decision here, not an oversight.

    `idempotency_key` makes payment *initiation* itself idempotent: POSTing
    to create a payment for a ride that already has one returns the
    existing Payment rather than erroring or creating a duplicate.
    """

    ride = models.OneToOneField("rides.Ride", on_delete=models.PROTECT, related_name="payment")
    fare = models.OneToOneField("pricing.Fare", on_delete=models.PROTECT, related_name="payment")
    method = models.CharField(max_length=20, choices=PaymentMethod.choices)
    provider_name = models.CharField(
        max_length=30, blank=True, help_text="Empty for CASH; e.g. 'stripe' or 'chapa' otherwise."
    )
    status = models.CharField(max_length=20, choices=PaymentStatus.choices, default=PaymentStatus.PENDING)
    currency = models.CharField(max_length=3)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    provider_reference = models.CharField(
        max_length=255, blank=True, db_index=True, help_text="The provider's own id for this charge."
    )
    idempotency_key = models.CharField(max_length=64, unique=True)
    failure_reason = models.CharField(max_length=255, blank=True)

    class Meta:
        db_table = "payments_payment"
        ordering = ["-created_at"]

    def __str__(self):
        return f"Payment<{self.ride_id}> {self.currency} {self.amount} [{self.status}]"


class PaymentTransactionType(models.TextChoices):
    AUTHORIZATION = "AUTHORIZATION", "Authorization"
    CAPTURE = "CAPTURE", "Capture"
    REFUND = "REFUND", "Refund"
    FAILURE = "FAILURE", "Failure"


class PaymentTransaction(BaseModel):
    """
    Append-only log of every state change a Payment goes through —
    immutable once created, per the architecture doc's rule for financial
    records ("never simply overwrite balances"). `idempotency_key` is what
    makes webhook processing safe against duplicate delivery: a unique
    constraint at the database level, not a check-then-act race, is what
    actually guarantees a retried webhook can never be recorded twice
    (see apps/payments/services.py:process_webhook).
    """

    payment = models.ForeignKey(Payment, on_delete=models.CASCADE, related_name="transactions")
    transaction_type = models.CharField(max_length=20, choices=PaymentTransactionType.choices)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    provider_reference = models.CharField(max_length=255, blank=True)
    idempotency_key = models.CharField(max_length=128, unique=True)
    raw_response = models.JSONField(null=True, blank=True, help_text="The provider's raw response/event, for audit.")
    initiated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="payment_transactions_initiated",
        help_text="Null for provider-webhook-driven transactions.",
    )

    class Meta:
        db_table = "payments_payment_transaction"
        ordering = ["created_at"]

    def __str__(self):
        return f"{self.transaction_type} {self.amount} on payment {self.payment_id}"
