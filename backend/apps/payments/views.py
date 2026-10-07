from rest_framework import permissions, status
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.identity.permissions import HasPermission
from apps.payments.models import Payment
from apps.payments.providers.base import InvalidWebhookSignature, PaymentProviderError
from apps.payments.serializers import InitiatePaymentSerializer, PaymentSerializer, RefundSerializer
from apps.payments.services import PaymentError, confirm_cash_payment, initiate_payment, process_webhook, refund_payment
from apps.rides.models import Ride


def _get_ride_or_404(pk):
    try:
        return Ride.objects.get(pk=pk)
    except Ride.DoesNotExist:
        raise NotFound("Ride not found.")


def _get_ride_by_token_or_404(tracking_token):
    try:
        return Ride.objects.get(tracking_token=tracking_token)
    except (Ride.DoesNotExist, ValueError, ValidationError):
        raise NotFound("No ride found for this tracking token.")


def _payment_error_response(exc: PaymentError) -> Response:
    return Response({"error": {"code": "payment_error", "message": str(exc), "details": None}}, status=status.HTTP_400_BAD_REQUEST)


class RidePaymentView(APIView):
    """
    GET  /api/v1/rides/{id}/payments/ — visible to the ride's own rider,
    its assigned driver, or `ride.manage` (same rule as ride detail).
    POST /api/v1/rides/{id}/payments/ — initiates payment; only the ride's
    own registered rider may call this.
    """

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, pk):
        ride = _get_ride_or_404(pk)
        self._require_access(request, ride)
        payment = Payment.objects.filter(ride=ride).prefetch_related("transactions").first()
        if payment is None:
            raise NotFound("No payment has been initiated for this ride yet.")
        return Response(PaymentSerializer(payment).data)

    def post(self, request, pk):
        ride = _get_ride_or_404(pk)
        if ride.rider_id != request.user.id:
            raise PermissionDenied("You are not the rider on this ride.")

        serializer = InitiatePaymentSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            payment = initiate_payment(ride, **serializer.validated_data)
        except PaymentError as exc:
            return _payment_error_response(exc)

        return Response(PaymentSerializer(payment).data, status=status.HTTP_201_CREATED)

    def _require_access(self, request, ride):
        from apps.identity.services import user_has_permission

        driver = getattr(request.user, "driver_profile", None)
        is_own_rider = ride.rider_id == request.user.id
        is_assigned_driver = driver is not None and ride.driver_id == driver.id
        is_admin_viewer = user_has_permission(request.user, "ride.manage")
        if not (is_own_rider or is_assigned_driver or is_admin_viewer):
            raise PermissionDenied("You do not have access to this ride.")


class GuestRidePaymentView(APIView):
    """Guest equivalent of RidePaymentView, authorized by tracking_token instead of a session."""

    permission_classes = [permissions.AllowAny]

    def get(self, request, tracking_token):
        ride = _get_ride_by_token_or_404(tracking_token)
        payment = Payment.objects.filter(ride=ride).prefetch_related("transactions").first()
        if payment is None:
            raise NotFound("No payment has been initiated for this ride yet.")
        return Response(PaymentSerializer(payment).data)

    def post(self, request, tracking_token):
        ride = _get_ride_by_token_or_404(tracking_token)
        serializer = InitiatePaymentSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            payment = initiate_payment(ride, **serializer.validated_data)
        except PaymentError as exc:
            return _payment_error_response(exc)

        return Response(PaymentSerializer(payment).data, status=status.HTTP_201_CREATED)


class ConfirmCashPaymentView(APIView):
    """POST /api/v1/rides/{id}/payments/confirm-cash/ — the assigned driver confirms cash received."""

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, pk):
        ride = _get_ride_or_404(pk)
        driver = getattr(request.user, "driver_profile", None)
        if driver is None:
            raise PermissionDenied("Only registered drivers may perform this action.")

        try:
            payment = confirm_cash_payment(ride, driver)
        except PaymentError as exc:
            return _payment_error_response(exc)

        return Response(PaymentSerializer(payment).data)


class PaymentWebhookView(APIView):
    """
    POST /api/v1/payments/webhooks/{provider_name}/ — public, but every
    request must carry a valid provider signature or it's rejected before
    any of its contents are trusted. This is the ONLY place a payment
    status can be set to CAPTURED/FAILED without either an authenticated
    driver (cash) or an admin (refund) taking an explicit action.
    """

    permission_classes = [permissions.AllowAny]
    _SIGNATURE_HEADERS = {"stripe": "HTTP_STRIPE_SIGNATURE", "chapa": "HTTP_CHAPA_SIGNATURE"}

    def post(self, request, provider_name):
        signature_header = request.META.get(self._SIGNATURE_HEADERS.get(provider_name, ""), "")

        try:
            newly_processed = process_webhook(
                provider_name, raw_body=request.body, signature_header=signature_header
            )
        except InvalidWebhookSignature:
            return Response(status=status.HTTP_400_BAD_REQUEST)
        except PaymentProviderError as exc:
            return Response({"error": {"code": "unknown_provider", "message": str(exc)}}, status=status.HTTP_400_BAD_REQUEST)
        except PaymentError as exc:
            # Genuinely unrecognized payment reference — log-worthy, but
            # still 200 so the provider doesn't retry indefinitely for a
            # webhook that will never resolve to anything on our side.
            import logging

            logging.getLogger(__name__).warning("Webhook for unknown payment: %s", exc)
            return Response(status=status.HTTP_200_OK)

        return Response({"processed": newly_processed}, status=status.HTTP_200_OK)


class RefundPaymentView(APIView):
    """POST /api/v1/admin/payments/{id}/refund/ — requires `payment.refund`."""

    permission_classes = [permissions.IsAuthenticated, HasPermission]
    required_permission = "payment.refund"

    def post(self, request, pk):
        try:
            payment = Payment.objects.get(pk=pk)
        except Payment.DoesNotExist:
            raise NotFound("Payment not found.")

        serializer = RefundSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            refund_payment(
                payment,
                admin_user=request.user,
                amount=serializer.validated_data.get("amount"),
                reason=serializer.validated_data["reason"],
            )
        except PaymentError as exc:
            return _payment_error_response(exc)

        payment.refresh_from_db()
        return Response(PaymentSerializer(payment).data)
