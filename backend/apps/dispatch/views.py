from django.utils import timezone
from rest_framework import generics, permissions
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.dispatch.models import DispatchOffer, DispatchOfferStatus
from apps.dispatch.serializers import DispatchOfferSerializer
from apps.identity.permissions import HasPermission


def _driver_or_403(request):
    driver = getattr(request.user, "driver_profile", None)
    if driver is None:
        raise PermissionDenied("Only registered drivers may perform this action.")
    return driver


class MyPendingOffersView(generics.ListAPIView):
    """
    GET /api/v1/drivers/me/offers/ — the calling driver's live ride offers.
    Polled for now (no WebSocket push for offers yet — Phase 5 adds
    real-time push for location and ride status only; offer delivery over
    the same channel is a natural follow-up but out of scope here).
    """

    permission_classes = [permissions.IsAuthenticated]
    serializer_class = DispatchOfferSerializer

    def get_queryset(self):
        driver = _driver_or_403(self.request)
        return DispatchOffer.objects.filter(
            driver=driver, status=DispatchOfferStatus.PENDING, expires_at__gt=timezone.now()
        ).select_related("ride")


class RunDispatchCycleView(APIView):
    """
    POST /api/v1/admin/dispatch/run-cycle/ — manually triggers one dispatch
    cycle (expire overdue offers, dispatch/redispatch searching rides, give
    up on rides that have exhausted their search window). In production
    this runs on a schedule (Celery beat) — see
    apps/dispatch/management/commands/run_dispatch_cycle.py and the README.
    Exposed as an endpoint too so it can be tested and triggered without
    shell access to the server.
    """

    permission_classes = [permissions.IsAuthenticated, HasPermission]
    required_permission = "ride.assign"

    def post(self, request):
        from apps.dispatch.services import run_dispatch_cycle

        return Response(run_dispatch_cycle())
