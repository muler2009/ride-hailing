from rest_framework import generics, permissions
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.analytics.serializers import DailyOperationsRollupSerializer, DailyRevenueRollupSerializer
from apps.analytics.services import get_daily_rollups, get_operations_summary, get_revenue_summary
from apps.common.pagination import StandardResultsPagination
from apps.identity.permissions import HasPermission


def _int_param(request, name: str, default: int, maximum: int) -> int:
    """Same clamped-parsing convention as apps.admin_api.views — a caller can't request an unbounded window."""
    try:
        value = int(request.query_params.get(name, default))
    except (TypeError, ValueError):
        return default
    return max(1, min(value, maximum))


class DailyOperationsRollupListView(generics.ListAPIView):
    """
    GET /api/v1/analytics/operations/daily/?days=30 — one row per closed
    day: ride outcomes and dispatch health, trailing `days` days.

    Gated on `ride.manage`, same as admin_api's live dashboard and
    dispatch-health views — this is the historical view of the same
    operational facts a Support Agent or Operations Manager already
    monitors ride-by-ride and in the live dashboard.
    """

    permission_classes = [permissions.IsAuthenticated, HasPermission]
    required_permission = "ride.manage"
    serializer_class = DailyOperationsRollupSerializer
    pagination_class = StandardResultsPagination

    def get_queryset(self):
        return get_daily_rollups(days=_int_param(self.request, "days", 30, 365))


class OperationsSummaryView(APIView):
    """GET /api/v1/analytics/operations/summary/?days=30 — window totals, correctly rate-weighted (not day-averaged)."""

    permission_classes = [permissions.IsAuthenticated, HasPermission]
    required_permission = "ride.manage"

    def get(self, request):
        return Response(get_operations_summary(days=_int_param(request, "days", 30, 365)))


class DailyRevenueRollupListView(generics.ListAPIView):
    """
    GET /api/v1/analytics/revenue/daily/?days=30 — one row per closed
    day: revenue, commission, driver earnings, and that day's
    per-vehicle-type breakdown.

    Gated on `payment.refund`, same as admin_api's live revenue
    report — deliberately not `ride.manage`, unchanged from Phase 12's
    reasoning: a role that can legitimately monitor rides has no
    business reason to see platform revenue, historical or otherwise.
    """

    permission_classes = [permissions.IsAuthenticated, HasPermission]
    required_permission = "payment.refund"
    serializer_class = DailyRevenueRollupSerializer
    pagination_class = StandardResultsPagination

    def get_queryset(self):
        return get_daily_rollups(days=_int_param(self.request, "days", 30, 365)).prefetch_related(
            "vehicle_type_breakdowns__vehicle_type"
        )


class RevenueSummaryView(APIView):
    """GET /api/v1/analytics/revenue/summary/?days=30 — window financial totals plus an aggregated vehicle-type breakdown."""

    permission_classes = [permissions.IsAuthenticated, HasPermission]
    required_permission = "payment.refund"

    def get(self, request):
        return Response(get_revenue_summary(days=_int_param(request, "days", 30, 365)))
