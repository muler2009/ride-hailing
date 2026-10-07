"""
notify() creates the right Notification row(s) for an event — synchronous,
fast, just DB writes — and enqueues async delivery for each one
(FR-NOT-03: delivery must never block the triggering request). The actual
provider calls happen in deliver_notification(), run via a Celery task
(apps/notifications/tasks.py), never inline in the request/response path
that triggered the event.

Channel selection:
  - A registered recipient always gets an IN_APP row (no external cost,
    no preference to honor).
  - EMAIL/SMS/PUSH are created only if the recipient has that channel's
    preference enabled AND the contact info it needs (email, phone
    number, or push token respectively).
  - A guest (no recipient_user) only ever gets SMS, to guest_phone_number
    — there's no account to attach IN_APP/EMAIL to and no preference to
    check, matching the registered-vs-guest split established in Phase 3.
"""
from django.db import transaction
from django.utils import timezone

from apps.notifications.factory import get_push_provider, get_sms_provider
from apps.notifications.models import Notification, NotificationChannel, NotificationStatus
from apps.notifications.providers.base import NotificationProviderError


class NotificationError(Exception):
    pass


@transaction.atomic
def notify(*, event_type: str, title: str, body: str, recipient_user=None, guest_phone_number: str = "", ride=None) -> list:
    """
    Returns the list of newly created Notification rows (each already
    queued for async delivery). Idempotent per (recipient/guest,
    event_type, channel, ride) — a retried trigger for the same event on
    the same ride is a no-op for any channel already queued, enforced by
    the unique idempotency_key rather than a check-then-act read.
    """
    if bool(recipient_user) == bool(guest_phone_number):
        raise NotificationError("Exactly one of recipient_user or guest_phone_number must be given.")

    channels = _resolve_channels(recipient_user, guest_phone_number)
    created = []

    for channel in channels:
        idempotency_key = _idempotency_key(recipient_user, guest_phone_number, ride, event_type, channel)
        if Notification.objects.filter(idempotency_key=idempotency_key).exists():
            continue

        notification = Notification.objects.create(
            recipient_user=recipient_user,
            guest_phone_number=guest_phone_number,
            channel=channel,
            event_type=event_type,
            title=title,
            body=body,
            ride=ride,
            idempotency_key=idempotency_key,
        )
        created.append(notification)

    for notification in created:
        _enqueue_delivery(notification.id)

    return created


def _resolve_channels(recipient_user, guest_phone_number) -> list:
    if recipient_user is None:
        return [NotificationChannel.SMS] if guest_phone_number else []

    channels = [NotificationChannel.IN_APP]
    if recipient_user.notify_email and recipient_user.email:
        channels.append(NotificationChannel.EMAIL)
    if recipient_user.notify_sms and recipient_user.phone_number:
        channels.append(NotificationChannel.SMS)
    if recipient_user.notify_push and recipient_user.push_token:
        channels.append(NotificationChannel.PUSH)
    return channels


def _idempotency_key(recipient_user, guest_phone_number, ride, event_type, channel) -> str:
    who = f"user:{recipient_user.id}" if recipient_user else f"guest:{guest_phone_number}"
    ride_part = f"ride:{ride.id}" if ride else "ride:none"
    return f"{who}:{ride_part}:{event_type}:{channel}"


def _enqueue_delivery(notification_id) -> None:
    from apps.notifications.tasks import deliver_notification_task

    deliver_notification_task.delay(notification_id)


def deliver_notification(notification_id) -> Notification:
    """
    The actual send. Called by the Celery task, never directly from a
    request handler. Never raises past this point — a delivery failure
    updates the Notification's own status/failure_reason instead, since a
    failed SMS shouldn't crash a background worker or retry forever
    without a limit here (basic at-most-once delivery per Notification
    row; retry policy belongs to the Celery task, not this function).
    """
    notification = Notification.objects.get(id=notification_id)
    if notification.status != NotificationStatus.PENDING:
        return notification  # already handled — idempotent no-op

    try:
        if notification.channel == NotificationChannel.IN_APP:
            pass  # nothing to deliver — the row itself IS the notification
        elif notification.channel == NotificationChannel.EMAIL:
            _send_email(notification)
        elif notification.channel == NotificationChannel.SMS:
            _send_sms(notification)
        elif notification.channel == NotificationChannel.PUSH:
            _send_push(notification)
    except NotificationProviderError as exc:
        notification.status = NotificationStatus.FAILED
        notification.failure_reason = str(exc)
        notification.save(update_fields=["status", "failure_reason"])
        return notification

    notification.status = NotificationStatus.SENT
    notification.sent_at = timezone.now()
    notification.save(update_fields=["status", "sent_at"])
    return notification


def _send_email(notification: Notification) -> None:
    from django.conf import settings
    from django.core.mail import send_mail

    send_mail(
        subject=notification.title,
        message=notification.body,
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[notification.recipient_user.email],
        fail_silently=False,
    )


def _send_sms(notification: Notification) -> None:
    to = notification.guest_phone_number or notification.recipient_user.phone_number
    get_sms_provider().send_sms(to=to, message=f"{notification.title}: {notification.body}")


def _send_push(notification: Notification) -> None:
    get_push_provider().send_push(
        device_token=notification.recipient_user.push_token,
        title=notification.title,
        body=notification.body,
        data={
            "event_type": notification.event_type,
            "ride_id": str(notification.ride_id) if notification.ride_id else "",
        },
    )
