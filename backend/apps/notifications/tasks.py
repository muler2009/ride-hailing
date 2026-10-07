from celery import shared_task


@shared_task(bind=True, max_retries=3, default_retry_delay=30)
def deliver_notification_task(self, notification_id):
    """
    Thin Celery wrapper around apps.notifications.services.deliver_notification.
    deliver_notification() itself never raises (a provider failure updates
    the Notification's own status instead), so this task's retry policy
    exists for genuine infrastructure hiccups (e.g. the DB briefly
    unreachable when the task runs), not for provider-side send failures.
    """
    from apps.notifications.services import deliver_notification

    try:
        deliver_notification(notification_id)
    except Exception as exc:
        raise self.retry(exc=exc)
