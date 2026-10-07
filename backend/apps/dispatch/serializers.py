from rest_framework import serializers

from apps.dispatch.models import DispatchOffer


class DispatchOfferSerializer(serializers.ModelSerializer):
    ride_id = serializers.UUIDField(source="ride.id")
    pickup_address = serializers.CharField(source="ride.pickup_address")
    destination_address = serializers.CharField(source="ride.destination_address")

    class Meta:
        model = DispatchOffer
        fields = [
            "id",
            "ride_id",
            "pickup_address",
            "destination_address",
            "distance_km",
            "status",
            "expires_at",
            "created_at",
        ]
        read_only_fields = fields
