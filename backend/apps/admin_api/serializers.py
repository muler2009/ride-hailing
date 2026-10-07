from rest_framework import serializers

from apps.admin_api.models import AuditLog


class AuditLogSerializer(serializers.ModelSerializer):
    class Meta:
        model = AuditLog
        fields = ["id", "actor_email", "action", "target_type", "target_id", "reason", "changes", "created_at"]
        read_only_fields = fields


class LiveRideSerializer(serializers.Serializer):
    ride_id = serializers.CharField()
    status = serializers.CharField()
    rider = serializers.CharField()
    driver = serializers.CharField(allow_null=True)
    vehicle_type = serializers.CharField()
    pickup_address = serializers.CharField()
    destination_address = serializers.CharField()
    driver_latitude = serializers.FloatField(allow_null=True)
    driver_longitude = serializers.FloatField(allow_null=True)
    created_at = serializers.DateTimeField()
