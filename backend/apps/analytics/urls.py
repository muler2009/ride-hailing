from django.urls import path

from apps.analytics.views import (
    DailyOperationsRollupListView,
    DailyRevenueRollupListView,
    OperationsSummaryView,
    RevenueSummaryView,
)

urlpatterns = [
    path("analytics/operations/daily/", DailyOperationsRollupListView.as_view(), name="analytics_operations_daily"),
    path("analytics/operations/summary/", OperationsSummaryView.as_view(), name="analytics_operations_summary"),
    path("analytics/revenue/daily/", DailyRevenueRollupListView.as_view(), name="analytics_revenue_daily"),
    path("analytics/revenue/summary/", RevenueSummaryView.as_view(), name="analytics_revenue_summary"),
]
