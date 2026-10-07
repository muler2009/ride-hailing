from rest_framework import serializers

from apps.vehicles.models import Vehicle, VehicleDocument, VehicleDocumentType, VehicleType


class VehicleTypeSerializer(serializers.ModelSerializer):
    class Meta:
        model = VehicleType
        fields = ["id", "name", "description", "passenger_capacity"]
        read_only_fields = fields


class VehicleDocumentSerializer(serializers.ModelSerializer):
    class Meta:
        model = VehicleDocument
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


class VehicleSerializer(serializers.ModelSerializer):
    documents = VehicleDocumentSerializer(many=True, read_only=True)
    vehicle_type = VehicleTypeSerializer(read_only=True)

    class Meta:
        model = Vehicle
        fields = [
            "id",
            "vehicle_type",
            "make",
            "model",
            "year",
            "color",
            "license_plate",
            "is_active",
            "documents",
            "created_at",
        ]
        read_only_fields = ["id", "is_active", "documents", "created_at"]


class VehicleCreateSerializer(serializers.Serializer):
    vehicle_type_id = serializers.UUIDField()
    make = serializers.CharField(max_length=100)
    model = serializers.CharField(max_length=100)
    year = serializers.IntegerField(min_value=1980)
    color = serializers.CharField(max_length=50, required=False, allow_blank=True)
    license_plate = serializers.CharField(max_length=20)

    def validate_license_plate(self, value):
        value = value.upper().strip()
        if Vehicle.objects.filter(license_plate=value).exists():
            raise serializers.ValidationError("A vehicle with this license plate is already registered.")
        return value

    def validate_vehicle_type_id(self, value):
        if not VehicleType.objects.filter(id=value, is_active=True).exists():
            raise serializers.ValidationError("Unknown or inactive vehicle type.")
        return value


class VehicleDocumentUploadSerializer(serializers.Serializer):
    document_type = serializers.ChoiceField(choices=VehicleDocumentType.choices)
    file = serializers.FileField()
    expires_at = serializers.DateField(required=False, allow_null=True)
