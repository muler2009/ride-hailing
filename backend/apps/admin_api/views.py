from rest_framework import generics, permissions
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.admin_api.models import AuditLog
from apps.admin_api.serializers import AuditLogSerializer, LiveRideSerializer
from apps.admin_api.services import (
    get_dashboard_summary,
    get_dispatch_health,
    get_live_rides,
    get_revenue_report,
)
from apps.identity.permissions import HasPermission


def _int_param(request, name: str, default: int, maximum: int) -> int:
    """Clamped so a caller can't request an unbounded aggregation window."""
    try:
        value = int(request.query_params.get(name, default))
    except (TypeError, ValueError):
        return default
    return max(1, min(value, maximum))


class DashboardSummaryView(APIView):
    """
    GET /api/v1/admin/dashboard/?hours=24 — operational snapshot.

    Gated on `ride.manage`, the read-level operational permission
    (Operations Manager, Support Agent, Super Admin) rather than a new
    one — this view creates no new capability, it only aggregates what
    those roles can already see ride-by-ride.
    """

    permission_classes = [permissions.IsAuthenticated, HasPermission]
    required_permission = "ride.manage"

    def get(self, request):
        return Response(get_dashboard_summary(since_hours=_int_param(request, "hours", 24, 720)))


class LiveRidesView(APIView):
    """GET /api/v1/admin/live-rides/ — in-flight rides with live driver positions from Redis."""

    permission_classes = [permissions.IsAuthenticated, HasPermission]
    required_permission = "ride.manage"

    def get(self, request):
        return Response(LiveRideSerializer(get_live_rides(), many=True).data)


class DispatchHealthView(APIView):
    """GET /api/v1/admin/dispatch-health/?hours=24 — matching-quality metrics."""

    permission_classes = [permissions.IsAuthenticated, HasPermission]
    required_permission = "ride.manage"

    def get(self, request):
        return Response(get_dispatch_health(since_hours=_int_param(request, "hours", 24, 720)))


class RevenueReportView(APIView):
    """
    GET /api/v1/admin/revenue/?days=30 — financial rollup.

    Gated on `payment.refund` (Finance Admin, Super Admin) rather than
    `ride.manage`: a Support Agent who can legitimately monitor rides has
    no business reason to see platform revenue, and this codebase's RBAC
    has been permission-specific rather than role-tiered since Phase 1.
    """

    permission_classes = [permissions.IsAuthenticated, HasPermission]
    required_permission = "payment.refund"

    def get(self, request):
        return Response(get_revenue_report(days=_int_param(request, "days", 30, 365)))


class AuditLogListView(generics.ListAPIView):
    """
    GET /api/v1/admin/audit-log/?action=&target_type=&target_id= — FR-ADM-06.

    Gated on `user.manage` (Super Admin by default): the audit log records
    what other administrators did, so it shouldn't be readable by the
    same operational roles it holds accountable.
    """

    permission_classes = [permissions.IsAuthenticated, HasPermission]
    required_permission = "user.manage"
    serializer_class = AuditLogSerializer

    def get_queryset(self):
        qs = AuditLog.objects.select_related("actor")
        params = self.request.query_params
        if params.get("action"):
            qs = qs.filter(action=params["action"].upper())
        if params.get("target_type"):
            qs = qs.filter(target_type=params["target_type"])
        if params.get("target_id"):
            qs = qs.filter(target_id=params["target_id"])
        if params.get("actor_email"):
            qs = qs.filter(actor_email__icontains=params["actor_email"])
        return qs
