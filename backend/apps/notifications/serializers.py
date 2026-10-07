from rest_framework import serializers

from apps.notifications.models import Notification


class NotificationSerializer(serializers.ModelSerializer):
    ride_id = serializers.UUIDField(source="ride.id", allow_null=True, read_only=True)

    class Meta:
        model = Notification
        fields = ["id", "channel", "event_type", "title", "body", "status", "ride_id", "created_at", "sent_at"]
        read_only_fields = fields


class PushTokenSerializer(serializers.Serializer):
    push_token = serializers.CharField(max_length=255, allow_blank=True)


class NotificationPreferencesSerializer(serializers.Serializer):
    notify_email = serializers.BooleanField(required=False)
    notify_sms = serializers.BooleanField(required=False)
    notify_push = serializers.BooleanField(required=False)
