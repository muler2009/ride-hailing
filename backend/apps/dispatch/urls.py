from django.urls import path

from apps.dispatch.views import MyPendingOffersView, RunDispatchCycleView

urlpatterns = [
    path("drivers/me/offers/", MyPendingOffersView.as_view(), name="my_pending_offers"),
    path("admin/dispatch/run-cycle/", RunDispatchCycleView.as_view(), name="admin_run_dispatch_cycle"),
]
