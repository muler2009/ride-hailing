from rest_framework import generics, permissions, status
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.vehicles.models import Vehicle, VehicleType
from apps.vehicles.serializers import (
    VehicleCreateSerializer,
    VehicleDocumentSerializer,
    VehicleDocumentUploadSerializer,
    VehicleSerializer,
    VehicleTypeSerializer,
)
from apps.vehicles.services import register_vehicle, set_active_vehicle, submit_vehicle_document


def _driver_or_403(request):
    driver = getattr(request.user, "driver_profile", None)
    if driver is None:
        raise PermissionDenied("Only registered drivers may manage vehicles.")
    return driver


class VehicleTypeListView(generics.ListAPIView):
    """GET /api/v1/vehicle-types/ — public reference data for registration forms."""

    permission_classes = [permissions.AllowAny]
    serializer_class = VehicleTypeSerializer
    queryset = VehicleType.objects.filter(is_active=True)
    pagination_class = None


class MyVehiclesView(generics.ListCreateAPIView):
    """
    GET  /api/v1/vehicles/me/  — the authenticated driver's vehicles
    POST /api/v1/vehicles/me/  — register a new vehicle
    """

    permission_classes = [permissions.IsAuthenticated]
    serializer_class = VehicleSerializer

    def get_queryset(self):
        driver = _driver_or_403(self.request)
        return Vehicle.objects.filter(driver=driver)

    def create(self, request, *args, **kwargs):
        driver = _driver_or_403(request)
        serializer = VehicleCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        vehicle_type = VehicleType.objects.get(id=data["vehicle_type_id"])
        vehicle = register_vehicle(
            driver,
            vehicle_type=vehicle_type,
            make=data["make"],
            model=data["model"],
            year=data["year"],
            license_plate=data["license_plate"],
            color=data.get("color", ""),
        )
        return Response(VehicleSerializer(vehicle).data, status=status.HTTP_201_CREATED)


class ActivateVehicleView(APIView):
    """POST /api/v1/vehicles/{id}/activate/ — make this the driver's active vehicle."""

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, pk):
        driver = _driver_or_403(request)
        try:
            vehicle = Vehicle.objects.get(pk=pk, driver=driver)
        except Vehicle.DoesNotExist:
            raise ValidationError({"vehicle": "Vehicle not found for this driver."})

        vehicle = set_active_vehicle(driver, vehicle)
        return Response(VehicleSerializer(vehicle).data)


class VehicleDocumentUploadView(APIView):
    """POST /api/v1/vehicles/{id}/documents/ — upload/replace a compliance document."""

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, pk):
        driver = _driver_or_403(request)
        try:
            vehicle = Vehicle.objects.get(pk=pk, driver=driver)
        except Vehicle.DoesNotExist:
            raise ValidationError({"vehicle": "Vehicle not found for this driver."})

        serializer = VehicleDocumentUploadSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        document = submit_vehicle_document(vehicle, **serializer.validated_data)

        return Response(
            VehicleDocumentSerializer(document).data, status=status.HTTP_201_CREATED
        )
