from rest_framework import serializers

from apps.pricing.models import Fare, FareLineItem, PricingRule
from apps.vehicles.serializers import VehicleTypeSerializer


class PricingRuleSerializer(serializers.ModelSerializer):
    vehicle_type = VehicleTypeSerializer(read_only=True)

    class Meta:
        model = PricingRule
        fields = [
            "id",
            "vehicle_type",
            "currency",
            "base_fare",
            "per_km_rate",
            "per_minute_rate",
            "per_minute_waiting_rate",
            "minimum_fare",
            "cancellation_fee",
            "surge_multiplier",
            "tax_rate",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "vehicle_type", "created_at", "updated_at"]


class PricingRuleUpsertSerializer(serializers.Serializer):
    currency = serializers.CharField(max_length=3, default="USD")
    base_fare = serializers.DecimalField(max_digits=8, decimal_places=2, min_value=0)
    per_km_rate = serializers.DecimalField(max_digits=8, decimal_places=2, min_value=0)
    per_minute_rate = serializers.DecimalField(max_digits=8, decimal_places=2, min_value=0)
    per_minute_waiting_rate = serializers.DecimalField(max_digits=8, decimal_places=2, min_value=0, default=0)
    minimum_fare = serializers.DecimalField(max_digits=8, decimal_places=2, min_value=0)
    cancellation_fee = serializers.DecimalField(max_digits=8, decimal_places=2, min_value=0, default=0)
    surge_multiplier = serializers.DecimalField(max_digits=4, decimal_places=2, min_value=0, default=1)
    tax_rate = serializers.DecimalField(max_digits=5, decimal_places=4, min_value=0, max_value=1, default=0)


class FareLineItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = FareLineItem
        fields = ["item_type", "label", "amount", "sequence"]
        read_only_fields = fields


class FareSerializer(serializers.ModelSerializer):
    line_items = FareLineItemSerializer(many=True, read_only=True)

    class Meta:
        model = Fare
        fields = [
            "id",
            "fare_type",
            "currency",
            "distance_km",
            "duration_minutes",
            "total_amount",
            "line_items",
            "created_at",
        ]
        read_only_fields = fields
