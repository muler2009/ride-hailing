import logging

from apps.common.middleware import get_current_request_id


class RequestIDLogFilter(logging.Filter):
    """Attaches the current request's ID (or "-" outside a request, e.g. in a Celery task) to every log record."""

    def filter(self, record):
        record.request_id = get_current_request_id()
        return True
