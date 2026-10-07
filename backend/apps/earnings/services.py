"""
Two distinct idempotency/correctness guarantees, mirroring Phase 8's
payments patterns exactly:

  - Crediting a driver's earning for a ride is idempotent against retry
    (FR-EAR-05) via a unique constraint on WalletLedgerEntry.idempotency_key
    — not a check-then-act read, which would itself race.
  - A payout's debit is created under a row lock on the driver's Wallet
    (select_for_update), so two concurrent payout requests against the
    same balance can't both succeed — the second one recomputes the
    balance only after the first's debit is visible to it.

A wallet's balance is never stored — it's always
`sum(wallet.ledger_entries.amount)`, computed fresh every time
(get_wallet_balance). This is FR-EAR-03 exactly: "derive a wallet balance
as the sum of its ledger entries rather than storing a directly mutable
balance field."
"""
from django.conf import settings
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from apps.common.money import ZERO, as_decimal, quantize_money
from apps.earnings.models import (
    DriverEarning,
    Payout,
    PayoutStatus,
    Wallet,
    WalletLedgerEntry,
    WalletLedgerEntryType,
)


class EarningsError(Exception):
    pass


def get_or_create_wallet(driver) -> Wallet:
    wallet, _created = Wallet.objects.get_or_create(driver=driver)
    return wallet


def get_wallet_balance(wallet):
    total = wallet.ledger_entries.aggregate(total=Sum("amount"))["total"]
    return total if total is not None else ZERO


@transaction.atomic
def credit_driver_earning(ride) -> DriverEarning:
    """
    Called once a ride is PAID (see apps/rides/services.py:mark_paid).
    Idempotent: if this ride already has a DriverEarning, it's returned
    as-is rather than recomputed — the OneToOne constraint on
    DriverEarning.ride and the ledger entry's idempotency_key both back
    this, so a retried trigger can never double-credit.
    """
    existing = DriverEarning.objects.filter(ride=ride).first()
    if existing is not None:
        return existing

    driver = ride.driver
    if driver is None:
        raise EarningsError("Ride has no assigned driver to credit.")

    fare = ride.fares.filter(fare_type="FINAL").first()
    if fare is None:
        raise EarningsError("Ride has no final fare to split.")

    commission_rate = as_decimal(settings.PLATFORM_COMMISSION_RATE)
    fare_amount = fare.total_amount
    commission_amount = quantize_money(fare_amount * commission_rate)
    driver_earning_amount = fare_amount - commission_amount

    earning = DriverEarning.objects.create(
        ride=ride,
        driver=driver,
        currency=fare.currency,
        fare_amount=fare_amount,
        commission_rate=commission_rate,
        commission_amount=commission_amount,
        driver_earning_amount=driver_earning_amount,
    )

    wallet = get_or_create_wallet(driver)
    WalletLedgerEntry.objects.create(
        wallet=wallet,
        entry_type=WalletLedgerEntryType.CREDIT_EARNING,
        amount=driver_earning_amount,
        description=f"Earning for ride {ride.id}",
        ride=ride,
        idempotency_key=f"earning:{ride.id}",
    )

    return earning


@transaction.atomic
def request_payout(driver, *, amount=None) -> Payout:
    """
    Row-locks the driver's wallet for the duration of this transaction —
    the same select_for_update discipline used throughout this codebase
    (ride assignment, dispatch offers) — so two concurrent payout requests
    against the same balance can't both succeed.
    """
    if Payout.objects.filter(driver=driver, status=PayoutStatus.PENDING).exists():
        raise EarningsError("A payout is already pending — wait for it to complete before requesting another.")

    wallet = Wallet.objects.select_for_update().get(driver=driver)
    balance = get_wallet_balance(wallet)

    payout_amount = quantize_money(amount) if amount is not None else balance
    if payout_amount <= 0:
        raise EarningsError("Payout amount must be positive.")
    if payout_amount < settings.MINIMUM_PAYOUT_AMOUNT:
        raise EarningsError(f"Minimum payout amount is {settings.MINIMUM_PAYOUT_AMOUNT}.")
    if payout_amount > balance:
        raise EarningsError(f"Payout amount exceeds available balance ({balance}).")

    payout = Payout.objects.create(driver=driver, currency=wallet.currency, amount=payout_amount)
    WalletLedgerEntry.objects.create(
        wallet=wallet,
        entry_type=WalletLedgerEntryType.DEBIT_PAYOUT,
        amount=-payout_amount,
        description=f"Payout {payout.id} requested",
        payout=payout,
        idempotency_key=f"payout-debit:{payout.id}",
    )
    return payout


@transaction.atomic
def mark_payout_completed(payout: Payout, admin_user) -> Payout:
    if payout.status != PayoutStatus.PENDING:
        raise EarningsError(f"Cannot complete a payout in status {payout.status}.")

    payout.status = PayoutStatus.COMPLETED
    payout.processed_at = timezone.now()
    payout.processed_by = admin_user
    payout.save(update_fields=["status", "processed_at", "processed_by"])

    from apps.admin_api.audit import record_audit

    record_audit(
        actor=admin_user, action="PAYOUT_COMPLETED", target_type="Payout", target_id=payout.id,
        changes={"amount": str(payout.amount)},
    )

    try:
        from apps.notifications.services import notify

        notify(
            event_type="PAYOUT_COMPLETED",
            title="Payout sent",
            body=f"Your payout of {payout.currency} {payout.amount} has been sent.",
            recipient_user=payout.driver.user,
        )
    except Exception:
        import logging

        logging.getLogger(__name__).exception("Notification dispatch failed for payout %s", payout.id)

    return payout


@transaction.atomic
def mark_payout_failed(payout: Payout, admin_user, reason: str = "") -> Payout:
    """
    Reverses the original debit with a new credit entry — never by
    deleting or editing the debit itself, per the append-only ledger rule.
    """
    if payout.status != PayoutStatus.PENDING:
        raise EarningsError(f"Cannot fail a payout in status {payout.status}.")

    payout.status = PayoutStatus.FAILED
    payout.processed_at = timezone.now()
    payout.processed_by = admin_user
    payout.failure_reason = reason
    payout.save(update_fields=["status", "processed_at", "processed_by", "failure_reason"])

    wallet = Wallet.objects.select_for_update().get(driver=payout.driver)
    WalletLedgerEntry.objects.create(
        wallet=wallet,
        entry_type=WalletLedgerEntryType.CREDIT_PAYOUT_REVERSAL,
        amount=payout.amount,
        description=f"Reversal of failed payout {payout.id}: {reason}".strip(),
        payout=payout,
        idempotency_key=f"payout-reversal:{payout.id}",
    )

    from apps.admin_api.audit import record_audit

    record_audit(
        actor=admin_user, action="PAYOUT_FAILED", target_type="Payout", target_id=payout.id,
        reason=reason, changes={"amount": str(payout.amount), "reversed": True},
    )

    return payout
