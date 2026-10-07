from rest_framework import serializers

from apps.earnings.models import DriverEarning, Payout, WalletLedgerEntry


class WalletLedgerEntrySerializer(serializers.ModelSerializer):
    ride_id = serializers.UUIDField(source="ride.id", allow_null=True, read_only=True)
    payout_id = serializers.UUIDField(source="payout.id", allow_null=True, read_only=True)

    class Meta:
        model = WalletLedgerEntry
        fields = ["entry_type", "amount", "description", "ride_id", "payout_id", "created_at"]
        read_only_fields = fields


class WalletSerializer(serializers.Serializer):
    currency = serializers.CharField()
    balance = serializers.DecimalField(max_digits=10, decimal_places=2)
    ledger_entries = WalletLedgerEntrySerializer(many=True)


class DriverEarningSerializer(serializers.ModelSerializer):
    ride_id = serializers.UUIDField(source="ride.id", read_only=True)

    class Meta:
        model = DriverEarning
        fields = [
            "ride_id",
            "currency",
            "fare_amount",
            "commission_rate",
            "commission_amount",
            "driver_earning_amount",
            "created_at",
        ]
        read_only_fields = fields


class PayoutSerializer(serializers.ModelSerializer):
    class Meta:
        model = Payout
        fields = ["id", "currency", "amount", "status", "processed_at", "failure_reason", "created_at"]
        read_only_fields = fields


class RequestPayoutSerializer(serializers.Serializer):
    amount = serializers.DecimalField(max_digits=10, decimal_places=2, min_value=0, required=False, allow_null=True)


class FailPayoutSerializer(serializers.Serializer):
    reason = serializers.CharField(max_length=255)
