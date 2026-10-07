from django.conf import settings
from django.db import models

from apps.common.models import BaseModel


class Wallet(BaseModel):
    """
    One wallet per driver. Deliberately has NO balance field — per the
    architecture doc's rule ("never simply overwrite balances"), the
    balance is always derived by summing WalletLedgerEntry.amount
    (apps/earnings/services.py:get_wallet_balance), never stored and
    mutated directly.
    """

    driver = models.OneToOneField("drivers.Driver", on_delete=models.PROTECT, related_name="wallet")
    currency = models.CharField(max_length=3, default="USD")

    class Meta:
        db_table = "earnings_wallet"

    def __str__(self):
        return f"Wallet<{self.driver_id}>"


class WalletLedgerEntryType(models.TextChoices):
    CREDIT_EARNING = "CREDIT_EARNING", "Earning Credit"
    DEBIT_PAYOUT = "DEBIT_PAYOUT", "Payout Debit"
    CREDIT_PAYOUT_REVERSAL = "CREDIT_PAYOUT_REVERSAL", "Payout Reversal Credit"
    CREDIT_ADJUSTMENT = "CREDIT_ADJUSTMENT", "Manual Credit Adjustment"
    DEBIT_ADJUSTMENT = "DEBIT_ADJUSTMENT", "Manual Debit Adjustment"


class WalletLedgerEntry(BaseModel):
    """
    Append-only. Every balance change is a new row, never an edit to an
    existing one — including reversals, which are their own new credit
    entry rather than a deletion or mutation of the original debit.
    Amount is signed (positive for credits, negative for debits) so a
    wallet's balance is always exactly `sum(entries.amount)`, matching the
    same signed-line-item convention Phase 7's Fare model already
    established.

    `idempotency_key` is what makes crediting a driver's earning for a
    given ride safe against a retried trigger (FR-EAR-05: "ensure earnings
    calculation for a given ride is applied at most once") — enforced by
    the unique constraint below, the same database-level guarantee
    Phase 8 used for webhook idempotency, not a check-then-act read.
    """

    wallet = models.ForeignKey(Wallet, on_delete=models.PROTECT, related_name="ledger_entries")
    entry_type = models.CharField(max_length=30, choices=WalletLedgerEntryType.choices)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    description = models.CharField(max_length=255, blank=True)
    ride = models.ForeignKey(
        "rides.Ride", on_delete=models.SET_NULL, null=True, blank=True, related_name="wallet_ledger_entries"
    )
    payout = models.ForeignKey(
        "earnings.Payout", on_delete=models.SET_NULL, null=True, blank=True, related_name="ledger_entries"
    )
    idempotency_key = models.CharField(max_length=128, unique=True)

    class Meta:
        db_table = "earnings_wallet_ledger_entry"
        ordering = ["created_at"]

    def __str__(self):
        return f"{self.entry_type} {self.amount} on wallet {self.wallet_id}"


class DriverEarning(BaseModel):
    """
    One row per ride, snapshotting exactly how that ride's earning was
    split — the commission rate is captured at the time of the split
    (not looked up freshly later), so a subsequent rate change never
    retroactively changes what an already-completed ride's record says.
    """

    ride = models.OneToOneField("rides.Ride", on_delete=models.PROTECT, related_name="earning")
    driver = models.ForeignKey("drivers.Driver", on_delete=models.PROTECT, related_name="earnings")
    currency = models.CharField(max_length=3, default="USD")
    fare_amount = models.DecimalField(max_digits=10, decimal_places=2)
    commission_rate = models.DecimalField(max_digits=5, decimal_places=4)
    commission_amount = models.DecimalField(max_digits=10, decimal_places=2)
    driver_earning_amount = models.DecimalField(max_digits=10, decimal_places=2)

    class Meta:
        db_table = "earnings_driver_earning"
        ordering = ["-created_at"]

    def __str__(self):
        return f"Earning<{self.ride_id}> driver={self.driver_id} {self.driver_earning_amount}"


class PayoutStatus(models.TextChoices):
    PENDING = "PENDING", "Pending"
    COMPLETED = "COMPLETED", "Completed"
    FAILED = "FAILED", "Failed"


class Payout(BaseModel):
    """
    A driver's request to withdraw available wallet balance. The debit
    ledger entry is created at REQUEST time (reserving the funds so a
    second concurrent request can't draw against the same balance twice —
    see apps/earnings/services.py's use of select_for_update on the
    wallet), not at completion. If a payout later fails, the reversal is
    a new CREDIT_PAYOUT_REVERSAL ledger entry, never a deletion or edit
    of the original debit.
    """

    driver = models.ForeignKey("drivers.Driver", on_delete=models.PROTECT, related_name="payouts")
    currency = models.CharField(max_length=3, default="USD")
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    status = models.CharField(max_length=20, choices=PayoutStatus.choices, default=PayoutStatus.PENDING)
    processed_at = models.DateTimeField(null=True, blank=True)
    processed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="payouts_processed"
    )
    failure_reason = models.CharField(max_length=255, blank=True)

    class Meta:
        db_table = "earnings_payout"
        ordering = ["-created_at"]

    def __str__(self):
        return f"Payout<{self.driver_id}> {self.currency} {self.amount} [{self.status}]"
