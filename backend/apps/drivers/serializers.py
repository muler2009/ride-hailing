from rest_framework import serializers

from apps.drivers.models import Driver, DriverDocument, DriverDocumentType
from apps.vehicles.serializers import VehicleSerializer


class DriverDocumentSerializer(serializers.ModelSerializer):
    class Meta:
        model = DriverDocument
        fields = [
            "id",
            "document_type",
            "file",
            "status",
            "rejection_reason",
            "expires_at",
            "created_at",
        ]
        read_only_fields = ["id", "status", "rejection_reason", "created_at"]


class DriverApplicationSerializer(serializers.Serializer):
    """POST body for applying as a driver / resubmitting after rejection."""

    license_number = serializers.CharField(max_length=50)
    license_expiry = serializers.DateField()
    date_of_birth = serializers.DateField(required=False, allow_null=True)


class DriverAvailabilitySerializer(serializers.Serializer):
    available = serializers.BooleanField()


class DriverDocumentUploadSerializer(serializers.Serializer):
    document_type = serializers.ChoiceField(choices=DriverDocumentType.choices)
    file = serializers.FileField()
    expires_at = serializers.DateField(required=False, allow_null=True)


class DriverSerializer(serializers.ModelSerializer):
    documents = DriverDocumentSerializer(many=True, read_only=True)
    vehicles = VehicleSerializer(many=True, read_only=True)
    email = serializers.EmailField(source="user.email", read_only=True)

    class Meta:
        model = Driver
        fields = [
            "id",
            "email",
            "license_number",
            "license_expiry",
            "date_of_birth",
            "approval_status",
            "rejection_reason",
            "suspension_reason",
            "reviewed_at",
            "is_available",
            "average_rating",
            "documents",
            "vehicles",
            "created_at",
        ]
        read_only_fields = [
            "id",
            "email",
            "approval_status",
            "rejection_reason",
            "suspension_reason",
            "reviewed_at",
            "is_available",
            "average_rating",
            "documents",
            "vehicles",
            "created_at",
        ]


class DriverAdminActionSerializer(serializers.Serializer):
    """Shared body shape for reject/suspend actions, which require a reason."""

    reason = serializers.CharField(max_length=255, required=False, allow_blank=True)
