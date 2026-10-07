from django.urls import path

from apps.earnings.views import (
    AdminPayoutListView,
    CompletePayoutView,
    FailPayoutView,
    MyEarningsListView,
    MyPayoutsView,
    MyWalletView,
)

urlpatterns = [
    path("drivers/me/wallet/", MyWalletView.as_view(), name="my_wallet"),
    path("drivers/me/earnings/", MyEarningsListView.as_view(), name="my_earnings"),
    path("drivers/me/payouts/", MyPayoutsView.as_view(), name="my_payouts"),
    path("admin/payouts/", AdminPayoutListView.as_view(), name="admin_payout_list"),
    path("admin/payouts/<uuid:pk>/complete/", CompletePayoutView.as_view(), name="admin_payout_complete"),
    path("admin/payouts/<uuid:pk>/fail/", FailPayoutView.as_view(), name="admin_payout_fail"),
]
