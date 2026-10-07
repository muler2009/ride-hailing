from celery import shared_task
from django.utils import timezone


@shared_task(bind=True, max_retries=3, default_retry_delay=300)
def compute_daily_rollup_task(self, iso_date: str | None = None):
    """
    Computes (or recomputes) one day's rollup. Scheduled via Celery beat
    (see config/celery.py) to run shortly after UTC midnight for
    *yesterday* — the first moment that day is guaranteed fully closed —
    but also callable directly with an explicit `iso_date` for a one-off
    manual recompute (e.g. from the Django admin or a shell).

    Retries only guard genuine infrastructure hiccups (DB briefly
    unreachable, same reasoning as
    apps.notifications.tasks.deliver_notification_task) — the
    computation itself is a pure read-then-upsert with no partial-failure
    state to worry about, since compute_daily_rollup() is fully idempotent.
    """
    from datetime import date, timedelta

    from apps.analytics.services import compute_daily_rollup

    target_date = date.fromisoformat(iso_date) if iso_date else timezone.localdate() - timedelta(days=1)

    try:
        compute_daily_rollup(target_date)
    except ValueError:
        # target_date turned out not to be closed yet (e.g. a manually
        # supplied iso_date of today) — not an infrastructure failure,
        # so don't retry; let the caller see the real error.
        raise
    except Exception as exc:
        raise self.retry(exc=exc)
