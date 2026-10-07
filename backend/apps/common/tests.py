from unittest.mock import MagicMock, patch

from django.test import TestCase

from apps.common.logging_filters import RequestIDLogFilter
from apps.common.middleware import RequestIDMiddleware, get_current_request_id


class HealthEndpointTests(TestCase):
    """Phase 14 — the two Kubernetes probe endpoints. See apps/common/health.py's docstring for the liveness/readiness split."""

    def test_liveness_always_reports_ok_and_checks_nothing(self):
        response = self.client.get("/healthz/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})

    def test_readiness_reports_ok_when_dependencies_are_reachable(self):
        response = self.client.get("/readyz/")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["checks"]["database"], "ok")
        self.assertEqual(body["checks"]["redis"], "ok")

    def test_readiness_returns_503_when_redis_is_unreachable(self):
        broken_client = MagicMock()
        broken_client.ping.side_effect = ConnectionError("refused")
        with patch("apps.common.health._readiness_redis_client", return_value=broken_client):
            response = self.client.get("/readyz/")
        self.assertEqual(response.status_code, 503)
        body = response.json()
        self.assertEqual(body["status"], "unavailable")
        self.assertEqual(body["checks"]["database"], "ok")
        self.assertIn("error", body["checks"]["redis"])

    def test_readiness_still_reports_the_database_when_only_redis_is_down(self):
        """A partial outage should say exactly which dependency is unhealthy, not just 'something is wrong'."""
        broken_client = MagicMock()
        broken_client.ping.side_effect = TimeoutError("timed out")
        with patch("apps.common.health._readiness_redis_client", return_value=broken_client):
            response = self.client.get("/readyz/")
        self.assertEqual(response.json()["checks"], {"database": "ok", "redis": "error: TimeoutError"})


class MetricsEndpointTests(TestCase):
    def test_metrics_endpoint_is_reachable_and_unauthenticated(self):
        """No auth required by design — see config/urls.py's comment on why (network-layer restriction instead)."""
        response = self.client.get("/metrics")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"python_gc_objects_collected_total", response.content)


class RequestIDMiddlewareTests(TestCase):
    def test_generates_a_request_id_when_none_is_supplied(self):
        response = self.client.get("/healthz/")
        self.assertIn("X-Request-ID", response.headers)
        self.assertTrue(len(response.headers["X-Request-ID"]) > 0)

    def test_reuses_an_inbound_request_id_instead_of_generating_a_new_one(self):
        response = self.client.get("/healthz/", headers={"X-Request-ID": "caller-supplied-id-123"})
        self.assertEqual(response.headers["X-Request-ID"], "caller-supplied-id-123")

    def test_two_requests_get_two_different_generated_ids(self):
        first = self.client.get("/healthz/").headers["X-Request-ID"]
        second = self.client.get("/healthz/").headers["X-Request-ID"]
        self.assertNotEqual(first, second)

    def test_request_id_is_visible_to_application_code_via_get_current_request_id(self):
        from django.http import HttpResponse
        from django.test import RequestFactory

        seen = {}

        def get_response(request):
            seen["id"] = get_current_request_id()
            return HttpResponse("ok")

        middleware = RequestIDMiddleware(get_response)
        request = RequestFactory().get("/healthz/", headers={"X-Request-ID": "abc-123"})
        middleware(request)
        self.assertEqual(seen["id"], "abc-123")


class RequestIDLogFilterTests(TestCase):
    def test_filter_attaches_the_current_request_id_to_a_log_record(self):
        import logging

        record = logging.LogRecord("test", logging.INFO, __file__, 1, "message", None, None)
        RequestIDLogFilter().filter(record)
        # Outside any request/middleware, this should be the contextvar's
        # default rather than raising or leaving the attribute unset.
        self.assertEqual(record.request_id, get_current_request_id())
