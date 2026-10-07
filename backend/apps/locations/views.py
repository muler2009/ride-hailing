from rest_framework import generics, permissions, status
from rest_framework.exceptions import NotFound, PermissionDenied, Throttled, ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.locations.models import DriverLocation
from apps.locations.serializers import DriverLocationHistorySerializer, DriverLocationUpdateSerializer
from apps.locations.services import LocationUpdateThrottled, record_location_update


def _driver_or_403(request):
    driver = getattr(request.user, "driver_profile", None)
    if driver is None:
        raise PermissionDenied("Only registered drivers may perform this action.")
    return driver


class DriverLocationUpdateView(APIView):
    """
    POST /api/v1/drivers/me/location/ — the driver app calls this every
    3-5 seconds while online. Updates Redis immediately, broadcasts to any
    rider tracking the driver's active ride over WebSocket, and samples a
    persisted snapshot roughly every 30 seconds. Requires the driver to be
    online (`is_available=True`).
    """

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        driver = _driver_or_403(request)
        if not driver.is_available:
            raise ValidationError({"available": "Go online before sending location updates."})

        serializer = DriverLocationUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        try:
            record_location_update(
                driver,
                latitude=float(data["latitude"]),
                longitude=float(data["longitude"]),
                heading=data.get("heading"),
                speed=data.get("speed"),
                accuracy=data.get("accuracy"),
            )
        except LocationUpdateThrottled as exc:
            raise Throttled(detail=str(exc))

        return Response(status=status.HTTP_204_NO_CONTENT)


class RideLocationHistoryView(generics.ListAPIView):
    """
    GET /api/v1/rides/{id}/locations/ — the sampled location trail for a
    ride, for trip playback or dispute review. Same access rule as the
    ride detail view: the ride's own rider, its assigned driver, or
    `ride.manage`.
    """

    permission_classes = [permissions.IsAuthenticated]
    serializer_class = DriverLocationHistorySerializer
    pagination_class = None

    def get_queryset(self):
        from apps.identity.services import user_has_permission
        from apps.rides.models import Ride

        try:
            ride = Ride.objects.get(pk=self.kwargs["pk"])
        except Ride.DoesNotExist:
            raise NotFound("Ride not found.")

        driver = getattr(self.request.user, "driver_profile", None)
        is_own_rider = ride.rider_id == self.request.user.id
        is_assigned_driver = driver is not None and ride.driver_id == driver.id
        is_admin_viewer = user_has_permission(self.request.user, "ride.manage")

        if not (is_own_rider or is_assigned_driver or is_admin_viewer):
            raise PermissionDenied("You do not have access to this ride.")

        return DriverLocation.objects.filter(ride=ride)
