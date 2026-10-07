import re

from rest_framework import serializers

from apps.drivers.models import Driver
from apps.rides.models import Ride, RideStatusHistory
from apps.vehicles.models import VehicleType
from apps.vehicles.serializers import VehicleTypeSerializer

_PHONE_RE = re.compile(r"^\+?[1-9]\d{6,14}$")


class RideCreateSerializer(serializers.Serializer):
    """
    Shared create payload for both registered riders and guests. Which path
    applies is decided by the view from the request's auth state, not from
    a field in the body — an authenticated rider can't submit someone
    else's phone number to ride as a "guest", and a guest can't claim to be
    a particular registered rider without logging in.
    """

    vehicle_type_id = serializers.UUIDField()
    pickup_address = serializers.CharField(max_length=255)
    pickup_latitude = serializers.DecimalField(max_digits=9, decimal_places=6)
    pickup_longitude = serializers.DecimalField(max_digits=9, decimal_places=6)
    destination_address = serializers.CharField(max_length=255)
    destination_latitude = serializers.DecimalField(max_digits=9, decimal_places=6)
    destination_longitude = serializers.DecimalField(max_digits=9, decimal_places=6)

    # Guest-only fields — required when the caller is unauthenticated,
    # ignored (a rider rides as themselves) when authenticated.
    guest_phone_number = serializers.CharField(max_length=32, required=False, allow_blank=True)
    guest_name = serializers.CharField(max_length=150, required=False, allow_blank=True)

    def validate_vehicle_type_id(self, value):
        if not VehicleType.objects.filter(id=value, is_active=True).exists():
            raise serializers.ValidationError("Unknown or inactive vehicle type.")
        return value

    def validate_guest_phone_number(self, value):
        if value and not _PHONE_RE.match(value):
            raise serializers.ValidationError(
                "Enter a valid phone number in international format, e.g. +15551234567."
            )
        return value

    def validate(self, attrs):
        is_authenticated = bool(self.context.get("is_authenticated"))
        if is_authenticated:
            attrs.pop("guest_phone_number", None)
            attrs.pop("guest_name", None)
        else:
            if not attrs.get("guest_phone_number"):
                raise serializers.ValidationError(
                    {"guest_phone_number": "Required when requesting a ride without an account."}
                )
        return attrs


class RideStatusHistorySerializer(serializers.ModelSerializer):
    changed_by_email = serializers.SerializerMethodField()

    class Meta:
        model = RideStatusHistory
        fields = ["from_status", "to_status", "changed_by_email", "note", "created_at"]
        read_only_fields = fields

    def get_changed_by_email(self, obj):
        return obj.changed_by.email if obj.changed_by_id else None


class RideDriverSummarySerializer(serializers.Serializer):
    id = serializers.UUIDField()
    email = serializers.EmailField(source="user.email")
    phone_number = serializers.CharField(source="user.phone_number")


class RideSerializer(serializers.ModelSerializer):
    vehicle_type = VehicleTypeSerializer(read_only=True)
    driver = serializers.SerializerMethodField()
    rider_email = serializers.SerializerMethodField()
    tracking_token = serializers.SerializerMethodField()
    status_history = RideStatusHistorySerializer(many=True, read_only=True)

    class Meta:
        model = Ride
        fields = [
            "id",
            "status",
            "rider_email",
            "guest_name",
            "vehicle_type",
            "driver",
            "pickup_address",
            "pickup_latitude",
            "pickup_longitude",
            "destination_address",
            "destination_latitude",
            "destination_longitude",
            "estimated_distance_km",
            "estimated_duration_minutes",
            "tracking_token",
            "cancellation_reason",
            "driver_assigned_at",
            "driver_accepted_at",
            "trip_started_at",
            "trip_completed_at",
            "cancelled_at",
            "status_history",
            "created_at",
        ]
        read_only_fields = fields

    def get_driver(self, obj):
        if obj.driver_id is None:
            return None
        return RideDriverSummarySerializer(obj.driver).data

    def get_rider_email(self, obj):
        return obj.rider.email if obj.rider_id else None

    def get_tracking_token(self, obj):
        # Only surfaced to the ride's own creator (rider or guest) — never
        # to the assigned driver or an admin viewing the same ride, since
        # it doubles as a guest's unauthenticated access credential.
        return str(obj.tracking_token) if self.context.get("include_tracking_token") else None


class RideAssignDriverSerializer(serializers.Serializer):
    driver_id = serializers.UUIDField()

    def validate_driver_id(self, value):
        if not Driver.objects.filter(id=value).exists():
            raise serializers.ValidationError("Unknown driver.")
        return value


class RideCancelSerializer(serializers.Serializer):
    reason = serializers.CharField(max_length=255, required=False, allow_blank=True)


class GuestRideCancelSerializer(RideCancelSerializer):
    pass
