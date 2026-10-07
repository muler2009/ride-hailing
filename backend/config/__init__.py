# Ensures the Celery app is loaded whenever Django starts, so
# `@shared_task`-decorated functions throughout apps/ are registered.
from config.celery import app as celery_app

__all__ = ("celery_app",)
