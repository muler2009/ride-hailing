"""
JSON log formatter for production (Phase 14). Lives in config/, not
apps/common/, because it's Django-project wiring referenced only from
settings.LOGGING — no app imports it directly.
"""
import json
import logging


class JsonFormatter(logging.Formatter):
    """One JSON object per line: timestamp, level, logger name, message, request_id, and exception info if present."""

    def format(self, record):
        payload = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": getattr(record, "request_id", "-"),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload)
