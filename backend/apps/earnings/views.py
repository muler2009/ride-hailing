from rest_framework import generics, permissions, status
from rest_framework.exceptions import NotFound, PermissionDenied
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.earnings.models import DriverEarning, Payout
from apps.earnings.serializers import (
    DriverEarningSerializer,
    FailPayoutSerializer,
    PayoutSerializer,
    RequestPayoutSerializer,
    WalletSerializer,
)
from apps.earnings.services import (
    EarningsError,
    get_or_create_wallet,
    get_wallet_balance,
    mark_payout_completed,
    mark_payout_failed,
    request_payout,
)
from apps.identity.permissions import HasPermission


def _driver_or_403(request):
    driver = getattr(request.user, "driver_profile", None)
    if driver is None:
        raise PermissionDenied("Only registered drivers may perform this action.")
    return driver


def _earnings_error_response(exc: EarningsError) -> Response:
    return Response({"error": {"code": "earnings_error", "message": str(exc), "details": None}}, status=status.HTTP_400_BAD_REQUEST)


class MyWalletView(APIView):
    """GET /api/v1/drivers/me/wallet/ — the calling driver's own wallet balance and ledger history."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        driver = _driver_or_403(request)
        wallet = get_or_create_wallet(driver)
        balance = get_wallet_balance(wallet)
        data = {
            "currency": wallet.currency,
            "balance": balance,
            "ledger_entries": list(wallet.ledger_entries.select_related("ride", "payout").all()),
        }
        return Response(WalletSerializer(data).data)


class MyEarningsListView(generics.ListAPIView):
    """GET /api/v1/drivers/me/earnings/ — the calling driver's per-ride earnings history."""

    permission_classes = [permissions.IsAuthenticated]
    serializer_class = DriverEarningSerializer
    pagination_class = None

    def get_queryset(self):
        driver = _driver_or_403(self.request)
        return DriverEarning.objects.filter(driver=driver).select_related("ride")


class MyPayoutsView(APIView):
    """
    GET  /api/v1/drivers/me/payouts/ — the calling driver's payout history.
    POST /api/v1/drivers/me/payouts/ — request a new payout. Body: {"amount": optional}.
    """

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        driver = _driver_or_403(request)
        payouts = Payout.objects.filter(driver=driver)
        return Response(PayoutSerializer(payouts, many=True).data)

    def post(self, request):
        driver = _driver_or_403(request)
        serializer = RequestPayoutSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            payout = request_payout(driver, amount=serializer.validated_data.get("amount"))
        except EarningsError as exc:
            return _earnings_error_response(exc)

        return Response(PayoutSerializer(payout).data, status=status.HTTP_201_CREATED)


class AdminPayoutListView(generics.ListAPIView):
    """GET /api/v1/admin/payouts/?status=PENDING — requires `payout.manage`."""

    permission_classes = [permissions.IsAuthenticated, HasPermission]
    required_permission = "payout.manage"
    serializer_class = PayoutSerializer

    def get_queryset(self):
        qs = Payout.objects.select_related("driver__user")
        status_filter = self.request.query_params.get("status")
        if status_filter:
            qs = qs.filter(status=status_filter.upper())
        return qs


class CompletePayoutView(APIView):
    """POST /api/v1/admin/payouts/{id}/complete/ — requires `payout.manage`."""

    permission_classes = [permissions.IsAuthenticated, HasPermission]
    required_permission = "payout.manage"

    def post(self, request, pk):
        try:
            payout = Payout.objects.get(pk=pk)
        except Payout.DoesNotExist:
            raise NotFound("Payout not found.")

        try:
            payout = mark_payout_completed(payout, request.user)
        except EarningsError as exc:
            return _earnings_error_response(exc)

        return Response(PayoutSerializer(payout).data)


class FailPayoutView(APIView):
    """POST /api/v1/admin/payouts/{id}/fail/ — requires `payout.manage`. Reverses the debit."""

    permission_classes = [permissions.IsAuthenticated, HasPermission]
    required_permission = "payout.manage"

    def post(self, request, pk):
        try:
            payout = Payout.objects.get(pk=pk)
        except Payout.DoesNotExist:
            raise NotFound("Payout not found.")

        serializer = FailPayoutSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            payout = mark_payout_failed(payout, request.user, reason=serializer.validated_data["reason"])
        except EarningsError as exc:
            return _earnings_error_response(exc)

        return Response(PayoutSerializer(payout).data)
