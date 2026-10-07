"""
Celery app for the Ride-Hailing Platform. Notifications (Phase 10) are the
first feature needing genuine background processing — FR-NOT-03 requires
delivery to never block the triggering request, and a slow SMS/email/push
API call is exactly the kind of work that shouldn't happen inline.

The broker is Redis, not RabbitMQ (which the architecture doc's tech
stack lists) — Redis is already a running dependency for dispatch's geo
store and Channels' layer, so reusing it avoids introducing a second
message broker. Celery abstracts the broker choice entirely; swapping to
RabbitMQ later is a settings change, not a code change.
"""
import os

from celery import Celery
from celery.schedules import crontab

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

app = Celery("ride_hailing")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()

# Phase 13 — Analytics & Reporting. Runs at 00:15 UTC, 15 minutes after
# the day rolls over, so it targets *yesterday* (the compute_daily_rollup
# default when called with no argument) well after midnight rather than
# racing it. This is plain Celery beat (in-process schedule, no extra
# dependency) rather than django-celery-beat — a single fixed daily job
# doesn't need a database-editable schedule, and every other async task
# in this codebase (Phase 10's notifications) is already a plain
# `@shared_task` with no beat entry until now, so this introduces the
# smallest amount of new machinery that does the job.
app.conf.beat_schedule = {
    "analytics-compute-daily-rollup": {
        "task": "apps.analytics.tasks.compute_daily_rollup_task",
        "schedule": crontab(hour=0, minute=15),
    },
}
