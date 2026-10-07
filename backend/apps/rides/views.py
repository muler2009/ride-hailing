from rest_framework import generics, permissions, status
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.drivers.models import Driver
from apps.identity.permissions import HasPermission
from apps.maps.base import Coordinates
from apps.maps.factory import get_map_provider
from apps.rides.models import Ride
from apps.rides.serializers import (
    GuestRideCancelSerializer,
    RideAssignDriverSerializer,
    RideCancelSerializer,
    RideCreateSerializer,
    RideSerializer,
)
from apps.rides.services import (
    InvalidRideTransition,
    RidePermissionError,
    accept_ride,
    assign_driver,
    cancel_by_driver,
    cancel_by_rider,
    complete_trip,
    create_ride,
    decline_ride,
    expire_ride,
    mark_arrived,
    mark_arriving,
    mark_no_driver_found,
    start_trip,
)
from apps.vehicles.models import VehicleType


class CanRequestRide(permissions.BasePermission):
    """
    Allows the request through if either:
      - the caller is unauthenticated (a guest, proceeding with just a
        phone number), or
      - the caller is authenticated AND holds the `ride.request` permission
        (granted to the Rider role by default).

    An authenticated caller without `ride.request` (e.g. a Driver with no
    Rider role) is correctly denied — being logged in doesn't
    automatically grant rider capabilities, per the RBAC model.
    """

    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return True
        from apps.identity.services import user_has_permission

        return user_has_permission(request.user, "ride.request")


def _service_errors_to_response(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs), None
    except InvalidRideTransition as exc:
        return None, Response(
            {"error": {"code": "invalid_transition", "message": str(exc), "details": None}},
            status=status.HTTP_400_BAD_REQUEST,
        )
    except RidePermissionError as exc:
        return None, Response(
            {"error": {"code": "permission_denied", "message": str(exc), "details": None}},
            status=status.HTTP_403_FORBIDDEN,
        )
    except ValueError as exc:
        return None, Response(
            {"error": {"code": "invalid_request", "message": str(exc), "details": None}},
            status=status.HTTP_400_BAD_REQUEST,
        )


def _get_ride_or_404(pk):
    try:
        return Ride.objects.get(pk=pk)
    except Ride.DoesNotExist:
        raise NotFound("Ride not found.")


def _driver_or_403(request):
    driver = getattr(request.user, "driver_profile", None)
    if driver is None:
        raise PermissionDenied("Only registered drivers may perform this action.")
    return driver


class RideCreateView(APIView):
    """
    POST /api/v1/rides/ — request a ride.

    No account required: an unauthenticated caller supplies
    `guest_phone_number` and gets back a `tracking_token` to check status
    or cancel later. An authenticated caller with the `ride.request`
    permission rides as themselves; their `guest_phone_number` (if any) is
    ignored.
    """

    permission_classes = [CanRequestRide]

    def post(self, request):
        is_authenticated = request.user and request.user.is_authenticated
        serializer = RideCreateSerializer(
            data=request.data, context={"is_authenticated": is_authenticated}
        )
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        vehicle_type = VehicleType.objects.get(id=data["vehicle_type_id"])
        ride = create_ride(
            rider=request.user if is_authenticated else None,
            guest_phone_number=data.get("guest_phone_number", ""),
            guest_name=data.get("guest_name", ""),
            vehicle_type=vehicle_type,
            pickup_address=data["pickup_address"],
            pickup_latitude=data["pickup_latitude"],
            pickup_longitude=data["pickup_longitude"],
            destination_address=data["destination_address"],
            destination_latitude=data["destination_latitude"],
            destination_longitude=data["destination_longitude"],
        )

        # Best-effort: routing is a separate bounded context (see
        # apps/maps) and its unavailability should never break ride
        # creation itself — the ride is simply created without an
        # estimate, same tolerance as the dispatch attempt below.
        try:
            route = get_map_provider().get_route(
                origin=Coordinates(latitude=float(ride.pickup_latitude), longitude=float(ride.pickup_longitude)),
                destination=Coordinates(
                    latitude=float(ride.destination_latitude), longitude=float(ride.destination_longitude)
                ),
            )
            ride.estimated_distance_km = route.distance_km
            ride.estimated_duration_minutes = route.duration_minutes
            ride.save(update_fields=["estimated_distance_km", "estimated_duration_minutes"])

            # Chained best-effort: a fare estimate is only possible once a
            # route estimate exists. Pricing is its own bounded context
            # (see apps/pricing) — a missing pricing rule for this vehicle
            # type shouldn't break ride creation either.
            try:
                from apps.pricing.services import estimate_fare_for_ride

                estimate_fare_for_ride(ride)
            except Exception:
                import logging

                logging.getLogger(__name__).exception("Fare estimation failed for ride %s", ride.id)
        except Exception:
            import logging

            logging.getLogger(__name__).exception("Route estimation failed for ride %s", ride.id)

        # Best-effort: dispatch is a separate bounded context (see
        # apps/dispatch) and its unavailability should never break ride
        # creation itself — if it fails or finds nobody nearby right now,
        # the ride simply stays SEARCHING_DRIVER for the periodic dispatch
        # cycle (apps/dispatch management command) to retry.
        try:
            from apps.dispatch.services import dispatch_ride

            dispatch_ride(ride)
        except Exception:
            import logging

            logging.getLogger(__name__).exception("Initial dispatch attempt failed for ride %s", ride.id)

        return Response(
            RideSerializer(ride, context={"include_tracking_token": True}).data,
            status=status.HTTP_201_CREATED,
        )


class MyRidesListView(generics.ListAPIView):
    """GET /api/v1/rides/me/ — a registered rider's own ride history."""

    permission_classes = [permissions.IsAuthenticated]
    serializer_class = RideSerializer

    def get_queryset(self):
        return Ride.objects.filter(rider=self.request.user)

    def get_serializer_context(self):
        return {**super().get_serializer_context(), "include_tracking_token": True}


class RideDetailView(APIView):
    """
    GET /api/v1/rides/{id}/ — visible to the ride's own rider, its assigned
    driver, or anyone holding `ride.manage` (ops/support/admin).
    """

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, pk):
        ride = _get_ride_or_404(pk)
        from apps.identity.services import user_has_permission

        driver = getattr(request.user, "driver_profile", None)
        is_own_rider = ride.rider_id == request.user.id
        is_assigned_driver = driver is not None and ride.driver_id == driver.id
        is_admin_viewer = user_has_permission(request.user, "ride.manage")

        if not (is_own_rider or is_assigned_driver or is_admin_viewer):
            raise PermissionDenied("You do not have access to this ride.")

        return Response(
            RideSerializer(ride, context={"include_tracking_token": is_own_rider}).data
        )


class RideCancelView(APIView):
    """
    POST /api/v1/rides/{id}/cancel/ — cancels the ride as whichever party
    the caller is (the rider or the assigned driver); which one is
    determined from the ride's own data, not asserted by the client.
    """

    permission_classes = [permissions.IsAuthenticated, HasPermission]
    required_permission = "ride.cancel"

    def post(self, request, pk):
        ride = _get_ride_or_404(pk)
        serializer = RideCancelSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        reason = serializer.validated_data.get("reason", "")

        driver = getattr(request.user, "driver_profile", None)
        if ride.rider_id == request.user.id:
            result, error = _service_errors_to_response(
                cancel_by_rider, ride.id, rider=request.user, reason=reason
            )
        elif driver is not None and ride.driver_id == driver.id:
            result, error = _service_errors_to_response(cancel_by_driver, ride.id, driver, reason)
        else:
            raise PermissionDenied("You are not a party to this ride.")

        if error:
            return error
        return Response(RideSerializer(result, context={"include_tracking_token": ride.rider_id == request.user.id}).data)


# ---------------------------------------------------------------------------
# Guest self-service — no login, authenticated instead by possession of the
# tracking_token issued at creation time.
# ---------------------------------------------------------------------------

def _get_ride_by_token_or_404(tracking_token):
    try:
        return Ride.objects.get(tracking_token=tracking_token)
    except (Ride.DoesNotExist, ValueError, ValidationError):
        raise NotFound("No ride found for this tracking token.")


class GuestRideTrackView(APIView):
    """GET /api/v1/rides/track/{tracking_token}/ — public, token-scoped status check."""

    permission_classes = [permissions.AllowAny]

    def get(self, request, tracking_token):
        ride = _get_ride_by_token_or_404(tracking_token)
        return Response(RideSerializer(ride, context={"include_tracking_token": True}).data)


class GuestRideCancelView(APIView):
    """POST /api/v1/rides/track/{tracking_token}/cancel/ — public, token-scoped cancellation."""

    permission_classes = [permissions.AllowAny]

    def post(self, request, tracking_token):
        ride = _get_ride_by_token_or_404(tracking_token)
        serializer = GuestRideCancelSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        result, error = _service_errors_to_response(
            cancel_by_rider,
            ride.id,
            guest_phone_number=ride.guest_phone_number,
            reason=serializer.validated_data.get("reason", ""),
        )
        if error:
            return error
        return Response(RideSerializer(result, context={"include_tracking_token": True}).data)


# ---------------------------------------------------------------------------
# Driver-side lifecycle actions.
# ---------------------------------------------------------------------------

class _DriverRideActionView(APIView):
    permission_classes = [permissions.IsAuthenticated, HasPermission]
    service_fn = None

    def post(self, request, pk):
        driver = _driver_or_403(request)
        _get_ride_or_404(pk)  # 404 before attempting the transition
        result, error = _service_errors_to_response(self.service_fn, pk, driver)
        if error:
            return error
        return Response(RideSerializer(result).data)


class RideAcceptView(_DriverRideActionView):
    """POST /api/v1/rides/{id}/accept/"""

    required_permission = "ride.accept"
    service_fn = staticmethod(accept_ride)


class RideDeclineView(_DriverRideActionView):
    """POST /api/v1/rides/{id}/decline/ — returns the ride to SEARCHING_DRIVER."""

    required_permission = "ride.accept"
    service_fn = staticmethod(decline_ride)


class RideArrivingView(_DriverRideActionView):
    """POST /api/v1/rides/{id}/arriving/"""

    required_permission = "ride.accept"
    service_fn = staticmethod(mark_arriving)


class RideArrivedView(_DriverRideActionView):
    """POST /api/v1/rides/{id}/arrived/"""

    required_permission = "ride.accept"
    service_fn = staticmethod(mark_arrived)


class RideStartView(_DriverRideActionView):
    """POST /api/v1/rides/{id}/start/"""

    required_permission = "ride.start"
    service_fn = staticmethod(start_trip)


class RideCompleteView(_DriverRideActionView):
    """POST /api/v1/rides/{id}/complete/"""

    required_permission = "ride.complete"
    service_fn = staticmethod(complete_trip)


# ---------------------------------------------------------------------------
# Admin / operations — the Phase 3 stand-in for real dispatch (Phase 4), and
# ride monitoring.
# ---------------------------------------------------------------------------

class AdminRideListView(generics.ListAPIView):
    """GET /api/v1/admin/rides/?status=SEARCHING_DRIVER"""

    permission_classes = [permissions.IsAuthenticated, HasPermission]
    required_permission = "ride.manage"
    serializer_class = RideSerializer

    def get_queryset(self):
        qs = Ride.objects.all().select_related("rider", "driver__user", "vehicle_type")
        status_filter = self.request.query_params.get("status")
        if status_filter:
            qs = qs.filter(status=status_filter.upper())
        return qs


class AssignDriverView(APIView):
    """POST /api/v1/admin/rides/{id}/assign-driver/ — body: {"driver_id": "..."}"""

    permission_classes = [permissions.IsAuthenticated, HasPermission]
    required_permission = "ride.assign"

    def post(self, request, pk):
        _get_ride_or_404(pk)
        serializer = RideAssignDriverSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        driver = Driver.objects.get(id=serializer.validated_data["driver_id"])

        result, error = _service_errors_to_response(assign_driver, pk, driver, request.user)
        if error:
            return error
        return Response(RideSerializer(result).data)


class MarkNoDriverFoundView(APIView):
    """POST /api/v1/admin/rides/{id}/mark-no-driver-found/"""

    permission_classes = [permissions.IsAuthenticated, HasPermission]
    required_permission = "ride.assign"

    def post(self, request, pk):
        _get_ride_or_404(pk)
        result, error = _service_errors_to_response(mark_no_driver_found, pk, request.user)
        if error:
            return error
        return Response(RideSerializer(result).data)


class ExpireRideView(APIView):
    """POST /api/v1/admin/rides/{id}/expire/ — rider no-show at pickup."""

    permission_classes = [permissions.IsAuthenticated, HasPermission]
    required_permission = "ride.assign"

    def post(self, request, pk):
        _get_ride_or_404(pk)
        result, error = _service_errors_to_response(expire_ride, pk, request.user)
        if error:
            return error
        return Response(RideSerializer(result).data)
