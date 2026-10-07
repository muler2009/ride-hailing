from django.conf import settings
from django.db import models

from apps.common.models import BaseModel


class NotificationChannel(models.TextChoices):
    IN_APP = "IN_APP", "In-App"
    EMAIL = "EMAIL", "Email"
    SMS = "SMS", "SMS"
    PUSH = "PUSH", "Push"


class NotificationEventType(models.TextChoices):
    RIDE_REQUESTED = "RIDE_REQUESTED", "Ride Requested"
    RIDE_DRIVER_ASSIGNED = "RIDE_DRIVER_ASSIGNED", "Driver Assigned"
    RIDE_DRIVER_ARRIVING = "RIDE_DRIVER_ARRIVING", "Driver Arriving"
    RIDE_DRIVER_ARRIVED = "RIDE_DRIVER_ARRIVED", "Driver Arrived"
    RIDE_STARTED = "RIDE_STARTED", "Ride Started"
    RIDE_COMPLETED = "RIDE_COMPLETED", "Ride Completed"
    PAYMENT_COMPLETED = "PAYMENT_COMPLETED", "Payment Completed"
    PAYMENT_FAILED = "PAYMENT_FAILED", "Payment Failed"
    DRIVER_APPROVED = "DRIVER_APPROVED", "Driver Approved"
    DRIVER_SUSPENDED = "DRIVER_SUSPENDED", "Driver Suspended"
    PAYOUT_COMPLETED = "PAYOUT_COMPLETED", "Payout Completed"


class NotificationStatus(models.TextChoices):
    PENDING = "PENDING", "Pending"
    SENT = "SENT", "Sent"
    FAILED = "FAILED", "Failed"


class Notification(BaseModel):
    """
    One row per (recipient, channel) for a single event — an event
    fanning out to email + SMS + in-app is three rows, not one row with
    three flags, so each channel's delivery status/failure is tracked
    independently (an email bounce shouldn't hide that the SMS succeeded).

    Either `recipient_user` or `guest_phone_number` is set, matching the
    same registered-vs-guest split established for Ride in Phase 3 — a
    guest has no account to attach an in-app/email notification to, only
    a phone number reachable by SMS.

    `idempotency_key` prevents the same (ride, event_type, channel) combo
    from being queued twice if a triggering call is retried — the same
    database-unique-constraint pattern used for webhook and earnings
    idempotency in Phases 8-9, not a check-then-act read.
    """

    recipient_user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, null=True, blank=True, related_name="notifications"
    )
    guest_phone_number = models.CharField(max_length=32, blank=True)
    channel = models.CharField(max_length=10, choices=NotificationChannel.choices)
    event_type = models.CharField(max_length=30, choices=NotificationEventType.choices, db_index=True)
    title = models.CharField(max_length=200)
    body = models.CharField(max_length=1000)
    status = models.CharField(max_length=10, choices=NotificationStatus.choices, default=NotificationStatus.PENDING)
    failure_reason = models.CharField(max_length=255, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    ride = models.ForeignKey(
        "rides.Ride", on_delete=models.SET_NULL, null=True, blank=True, related_name="notifications"
    )
    idempotency_key = models.CharField(max_length=150, unique=True)

    class Meta:
        db_table = "notifications_notification"
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["recipient_user", "status"])]

    def __str__(self):
        who = self.recipient_user_id or self.guest_phone_number
        return f"Notification<{who}> {self.channel}/{self.event_type} [{self.status}]"
