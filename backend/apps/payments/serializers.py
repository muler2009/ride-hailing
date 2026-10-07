from rest_framework import serializers

from apps.payments.models import Payment, PaymentMethod, PaymentTransaction


class InitiatePaymentSerializer(serializers.Serializer):
    method = serializers.ChoiceField(choices=PaymentMethod.choices)
    provider_name = serializers.CharField(required=False, allow_blank=True, default="")
    payment_token = serializers.CharField(required=False, allow_blank=True, default=None, allow_null=True)

    def validate(self, attrs):
        if attrs["method"] != PaymentMethod.CASH and not attrs.get("provider_name"):
            raise serializers.ValidationError({"provider_name": "Required for non-cash payment methods."})
        return attrs


class PaymentTransactionSerializer(serializers.ModelSerializer):
    class Meta:
        model = PaymentTransaction
        fields = ["transaction_type", "amount", "provider_reference", "created_at"]
        read_only_fields = fields


class PaymentSerializer(serializers.ModelSerializer):
    transactions = PaymentTransactionSerializer(many=True, read_only=True)

    class Meta:
        model = Payment
        fields = [
            "id",
            "method",
            "provider_name",
            "status",
            "currency",
            "amount",
            "provider_reference",
            "failure_reason",
            "transactions",
            "created_at",
        ]
        read_only_fields = fields


class RefundSerializer(serializers.Serializer):
    amount = serializers.DecimalField(max_digits=10, decimal_places=2, min_value=0, required=False, allow_null=True)
    reason = serializers.CharField(max_length=255)
