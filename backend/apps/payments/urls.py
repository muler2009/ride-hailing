from django.urls import path

from apps.payments.views import (
    ConfirmCashPaymentView,
    GuestRidePaymentView,
    PaymentWebhookView,
    RefundPaymentView,
    RidePaymentView,
)

urlpatterns = [
    path("rides/<uuid:pk>/payments/", RidePaymentView.as_view(), name="ride_payment"),
    path("rides/<uuid:pk>/payments/confirm-cash/", ConfirmCashPaymentView.as_view(), name="confirm_cash_payment"),
    path(
        "rides/track/<uuid:tracking_token>/payments/",
        GuestRidePaymentView.as_view(),
        name="guest_ride_payment",
    ),
    path("payments/webhooks/<str:provider_name>/", PaymentWebhookView.as_view(), name="payment_webhook"),
    path("admin/payments/<uuid:pk>/refund/", RefundPaymentView.as_view(), name="admin_payment_refund"),
]
