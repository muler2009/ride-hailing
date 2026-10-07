from rest_framework import generics, permissions, status
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.drivers.models import Driver
from apps.drivers.serializers import (
    DriverAdminActionSerializer,
    DriverApplicationSerializer,
    DriverAvailabilitySerializer,
    DriverDocumentSerializer,
    DriverDocumentUploadSerializer,
    DriverSerializer,
)
from apps.drivers.services import (
    InvalidDriverTransition,
    apply_as_driver,
    approve_driver,
    reactivate_driver,
    reject_driver,
    set_availability,
    submit_document,
    suspend_driver,
)
from apps.identity.permissions import HasPermission


class MyDriverProfileView(APIView):
    """
    GET  /api/v1/drivers/me/  — the authenticated user's driver profile
    POST /api/v1/drivers/me/  — apply as a driver, or resubmit after rejection
    """

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        try:
            driver = request.user.driver_profile
        except Driver.DoesNotExist:
            return Response(
                {"error": {"code": "not_found", "message": "No driver application on file.", "details": None}},
                status=status.HTTP_404_NOT_FOUND,
            )
        return Response(DriverSerializer(driver).data)

    def post(self, request):
        serializer = DriverApplicationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        driver = apply_as_driver(request.user, **serializer.validated_data)
        return Response(DriverSerializer(driver).data, status=status.HTTP_200_OK)


class DriverAvailabilityView(APIView):
    """POST /api/v1/drivers/me/availability/ — online/offline toggle. body: {"available": true}"""

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        try:
            driver = request.user.driver_profile
        except Driver.DoesNotExist:
            raise ValidationError({"driver": "You must be a registered driver to set availability."})

        serializer = DriverAvailabilitySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            driver = set_availability(driver, serializer.validated_data["available"])
        except ValueError as exc:
            raise ValidationError({"available": str(exc)})

        return Response(DriverSerializer(driver).data)


class DriverDocumentUploadView(APIView):
    """POST /api/v1/drivers/me/documents/ — upload/replace a KYC document."""

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        try:
            driver = request.user.driver_profile
        except Driver.DoesNotExist:
            raise ValidationError(
                {"driver": "You must submit a driver application before uploading documents."}
            )

        serializer = DriverDocumentUploadSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        document = submit_document(driver, **serializer.validated_data)

        return Response(DriverDocumentSerializer(document).data, status=status.HTTP_201_CREATED)


# ---------------------------------------------------------------------------
# Administrative approval workflow — all gated on RBAC permissions, never on
# `request.user.is_staff` or a hardcoded role check.
# ---------------------------------------------------------------------------

class AdminDriverListView(generics.ListAPIView):
    """GET /api/v1/admin/drivers/?status=PENDING_REVIEW — queue for review."""

    permission_classes = [permissions.IsAuthenticated, HasPermission]
    required_permission = "driver.approve"
    serializer_class = DriverSerializer

    def get_queryset(self):
        qs = Driver.objects.all().select_related("user")
        status_filter = self.request.query_params.get("status")
        if status_filter:
            qs = qs.filter(approval_status=status_filter.upper())
        return qs


class _DriverAdminActionView(APIView):
    permission_classes = [permissions.IsAuthenticated, HasPermission]

    def get_driver(self, pk):
        try:
            return Driver.objects.get(pk=pk)
        except Driver.DoesNotExist:
            raise ValidationError({"driver": "Driver not found."})

    def handle_transition(self, fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except InvalidDriverTransition as exc:
            raise ValidationError({"approval_status": str(exc)})


class ApproveDriverView(_DriverAdminActionView):
    """POST /api/v1/admin/drivers/{id}/approve/"""

    required_permission = "driver.approve"

    def post(self, request, pk):
        driver = self.get_driver(pk)
        driver = self.handle_transition(approve_driver, driver, request.user)
        return Response(DriverSerializer(driver).data)


class RejectDriverView(_DriverAdminActionView):
    """POST /api/v1/admin/drivers/{id}/reject/  body: {"reason": "..."}"""

    required_permission = "driver.approve"

    def post(self, request, pk):
        driver = self.get_driver(pk)
        serializer = DriverAdminActionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        driver = self.handle_transition(
            reject_driver, driver, request.user, serializer.validated_data.get("reason", "")
        )
        return Response(DriverSerializer(driver).data)


class SuspendDriverView(_DriverAdminActionView):
    """POST /api/v1/admin/drivers/{id}/suspend/  body: {"reason": "..."}"""

    required_permission = "driver.suspend"

    def post(self, request, pk):
        driver = self.get_driver(pk)
        serializer = DriverAdminActionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        driver = self.handle_transition(
            suspend_driver, driver, request.user, serializer.validated_data.get("reason", "")
        )
        return Response(DriverSerializer(driver).data)


class ReactivateDriverView(_DriverAdminActionView):
    """POST /api/v1/admin/drivers/{id}/reactivate/"""

    required_permission = "driver.suspend"

    def post(self, request, pk):
        driver = self.get_driver(pk)
        driver = self.handle_transition(reactivate_driver, driver, request.user)
        return Response(DriverSerializer(driver).data)
