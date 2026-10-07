from rest_framework import serializers

from apps.locations.models import DriverLocation


class DriverLocationUpdateSerializer(serializers.Serializer):
    latitude = serializers.DecimalField(max_digits=9, decimal_places=6, min_value=-90, max_value=90)
    longitude = serializers.DecimalField(max_digits=9, decimal_places=6, min_value=-180, max_value=180)
    heading = serializers.DecimalField(
        max_digits=5, decimal_places=2, min_value=0, max_value=360, required=False, allow_null=True
    )
    speed = serializers.DecimalField(max_digits=6, decimal_places=2, min_value=0, required=False, allow_null=True)
    accuracy = serializers.DecimalField(max_digits=6, decimal_places=2, min_value=0, required=False, allow_null=True)


class DriverLocationHistorySerializer(serializers.ModelSerializer):
    class Meta:
        model = DriverLocation
        fields = ["latitude", "longitude", "heading", "speed", "accuracy", "recorded_at"]
        read_only_fields = fields
