from django.urls import path

from apps.admin_api.views import (
    AuditLogListView,
    DashboardSummaryView,
    DispatchHealthView,
    LiveRidesView,
    RevenueReportView,
)

urlpatterns = [
    path("admin/dashboard/", DashboardSummaryView.as_view(), name="admin_dashboard"),
    path("admin/live-rides/", LiveRidesView.as_view(), name="admin_live_rides"),
    path("admin/dispatch-health/", DispatchHealthView.as_view(), name="admin_dispatch_health"),
    path("admin/revenue/", RevenueReportView.as_view(), name="admin_revenue"),
    path("admin/audit-log/", AuditLogListView.as_view(), name="admin_audit_log"),
]
