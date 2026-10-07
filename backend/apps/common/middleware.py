"""
Request-ID propagation for log correlation (Phase 14).

Generates a UUID per request — or reuses an inbound `X-Request-ID` from
the Ingress, so a request's ID stays the same end-to-end across the
Ingress's own logs and this app's — and stores it in a contextvar so
apps/common/logging_filters.py can attach it to every log line emitted
while handling that request, including ones several function calls deep
in a service module with no request object threaded through. Echoed back
on the response so the client (or the Ingress access log) can correlate
against it too.

A contextvar rather than thread-local storage: this process is served by
both Gunicorn (sync, one thread per request) and Daphne/Channels (async,
many requests interleaved on one thread), and only contextvars give
correct per-request isolation in the async case.
"""
import uuid
from contextvars import ContextVar

_request_id_var: ContextVar[str] = ContextVar("request_id", default="-")


def get_current_request_id() -> str:
    return _request_id_var.get()


class RequestIDMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex
        # Deliberately NOT reset in a `finally` after get_response(): Django
        # logs 4xx/5xx responses (django.utils.log.log_response) *after*
        # the middleware chain has already unwound past this point, so a
        # reset here would clear the ID before that log line — the exact
        # line most worth correlating — ever sees it. Every request sets
        # its own fresh ID unconditionally as the first thing that happens
        # here, so a value left over on a reused WSGI worker thread is
        # always overwritten before it could be attributed to the wrong
        # request; nothing ever reads it in the idle gap between requests.
        _request_id_var.set(request_id)
        request.request_id = request_id
        response = self.get_response(request)
        response["X-Request-ID"] = request_id
        return response
