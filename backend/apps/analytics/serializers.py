from rest_framework import serializers

from apps.analytics.models import DailyPlatformRollup, DailyVehicleTypeRollup


class DailyOperationsRollupSerializer(serializers.ModelSerializer):
    """One historical day's ride/dispatch-health numbers — the trend-list counterpart to admin_api's live dashboard/dispatch-health views."""

    offer_acceptance_rate = serializers.FloatField(read_only=True)
    no_driver_found_rate = serializers.FloatField(read_only=True)

    class Meta:
        model = DailyPlatformRollup
        fields = [
            "date",
            "rides_requested",
            "rides_completed",
            "rides_cancelled",
            "rides_no_driver_found",
            "active_drivers",
            "offers_sent",
            "offers_accepted",
            "offers_expired",
            "offers_declined",
            "offer_acceptance_rate",
            "no_driver_found_rate",
            "payment_failures",
        ]
        read_only_fields = fields


class VehicleTypeRollupSerializer(serializers.ModelSerializer):
    vehicle_type = serializers.CharField(source="vehicle_type.name")

    class Meta:
        model = DailyVehicleTypeRollup
        fields = ["vehicle_type", "ride_count", "gross_revenue", "platform_commission"]
        read_only_fields = fields


class DailyRevenueRollupSerializer(serializers.ModelSerializer):
    """One historical day's financial numbers plus that day's per-vehicle-type breakdown."""

    by_vehicle_type = VehicleTypeRollupSerializer(source="vehicle_type_breakdowns", many=True, read_only=True)

    class Meta:
        model = DailyPlatformRollup
        fields = [
            "date",
            "rides_with_earning",
            "gross_revenue",
            "platform_commission",
            "driver_earnings",
            "average_fare",
            "by_vehicle_type",
        ]
        read_only_fields = fields
