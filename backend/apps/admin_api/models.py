from django.conf import settings
from django.db import models

from apps.common.models import BaseModel


class AuditAction(models.TextChoices):
    DRIVER_APPROVED = "DRIVER_APPROVED", "Driver Approved"
    DRIVER_REJECTED = "DRIVER_REJECTED", "Driver Rejected"
    DRIVER_SUSPENDED = "DRIVER_SUSPENDED", "Driver Suspended"
    DRIVER_REACTIVATED = "DRIVER_REACTIVATED", "Driver Reactivated"
    PRICING_RULE_UPDATED = "PRICING_RULE_UPDATED", "Pricing Rule Updated"
    PAYMENT_REFUNDED = "PAYMENT_REFUNDED", "Payment Refunded"
    PAYOUT_COMPLETED = "PAYOUT_COMPLETED", "Payout Completed"
    PAYOUT_FAILED = "PAYOUT_FAILED", "Payout Failed"
    RIDE_DRIVER_ASSIGNED = "RIDE_DRIVER_ASSIGNED", "Ride Driver Manually Assigned"
    USER_SUSPENDED = "USER_SUSPENDED", "User Suspended"


class AuditLog(BaseModel):
    """
    FR-ADM-06: "maintain an audit log of all administrative actions
    affecting users, pricing, and financial records."

    Append-only and immutable, like every other accountability record in
    this codebase (RideStatusHistory, PaymentTransaction,
    WalletLedgerEntry) — there is no update or delete path in the service
    layer, and the admin interface registers it read-only. An audit log
    an administrator can edit is not an audit log.

    `target_type`/`target_id` are stored as plain strings rather than a
    GenericForeignKey deliberately: an audit entry must survive the thing
    it describes being deleted. A GFK would either cascade the log away
    or leave a dangling reference; a recorded string keeps reading
    correctly forever.

    `changes` holds a small before/after dict where meaningful (e.g. a
    pricing rate change), and is left empty for actions where the action
    name and reason already say everything.
    """

    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="audit_entries",
        help_text="Null if the acting account was later deleted — the entry itself still stands.",
    )
    actor_email = models.CharField(
        max_length=254, blank=True, help_text="Snapshotted at write time, so the entry stays readable if the account goes away."
    )
    action = models.CharField(max_length=40, choices=AuditAction.choices, db_index=True)
    target_type = models.CharField(max_length=50, help_text="e.g. 'Driver', 'PricingRule', 'Payment'.")
    target_id = models.CharField(max_length=64)
    reason = models.CharField(max_length=255, blank=True)
    changes = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = "admin_api_audit_log"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["target_type", "target_id"]),
            models.Index(fields=["actor", "created_at"]),
        ]

    def __str__(self):
        return f"{self.actor_email or 'system'} {self.action} {self.target_type}:{self.target_id}"
