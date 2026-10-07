from rest_framework import permissions
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.maps.base import Coordinates, MapProviderError
from apps.maps.factory import get_map_provider
from apps.maps.serializers import (
    GeocodeRequestSerializer,
    GeocodeResponseSerializer,
    ReverseGeocodeRequestSerializer,
    ReverseGeocodeResponseSerializer,
    RouteRequestSerializer,
    RouteResponseSerializer,
)


def _map_provider_error_response(exc: MapProviderError) -> Response:
    return Response(
        {"error": {"code": "map_provider_error", "message": str(exc), "details": None}}, status=422
    )


class GeocodeView(APIView):
    """
    POST /api/v1/maps/geocode/ — public. Address -> coordinates, for an
    address search box. No account required, same as ride creation itself —
    a guest planning a ride needs to search addresses before they can even
    submit a pickup/destination.
    """

    permission_classes = [permissions.AllowAny]

    def post(self, request):
        serializer = GeocodeRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            result = get_map_provider().geocode(serializer.validated_data["address"])
        except MapProviderError as exc:
            return _map_provider_error_response(exc)

        return Response(
            GeocodeResponseSerializer(
                {
                    "formatted_address": result.formatted_address,
                    "latitude": result.coordinates.latitude,
                    "longitude": result.coordinates.longitude,
                }
            ).data
        )


class ReverseGeocodeView(APIView):
    """POST /api/v1/maps/reverse-geocode/ — public. Coordinates -> a human-readable address."""

    permission_classes = [permissions.AllowAny]

    def post(self, request):
        serializer = ReverseGeocodeRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        try:
            address = get_map_provider().reverse_geocode(
                Coordinates(latitude=data["latitude"], longitude=data["longitude"])
            )
        except MapProviderError as exc:
            return _map_provider_error_response(exc)

        return Response(ReverseGeocodeResponseSerializer({"formatted_address": address}).data)


class RouteView(APIView):
    """
    POST /api/v1/maps/route/ — public. Origin/destination -> distance,
    duration, and a polyline for map display. Useful for showing an ETA
    before confirming a ride request (fare estimation itself is Phase 7).
    """

    permission_classes = [permissions.AllowAny]

    def post(self, request):
        serializer = RouteRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        try:
            route = get_map_provider().get_route(
                origin=Coordinates(latitude=data["origin_latitude"], longitude=data["origin_longitude"]),
                destination=Coordinates(
                    latitude=data["destination_latitude"], longitude=data["destination_longitude"]
                ),
            )
        except MapProviderError as exc:
            return _map_provider_error_response(exc)

        return Response(
            RouteResponseSerializer(
                {
                    "distance_km": route.distance_km,
                    "duration_minutes": route.duration_minutes,
                    "polyline": route.polyline,
                }
            ).data
        )
