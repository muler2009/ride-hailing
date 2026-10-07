from rest_framework import generics, permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.notifications.models import Notification, NotificationChannel
from apps.notifications.serializers import NotificationPreferencesSerializer, NotificationSerializer, PushTokenSerializer


class MyNotificationsListView(generics.ListAPIView):
    """GET /api/v1/notifications/me/ — the caller's in-app notification feed."""

    permission_classes = [permissions.IsAuthenticated]
    serializer_class = NotificationSerializer
    pagination_class = None

    def get_queryset(self):
        return Notification.objects.filter(
            recipient_user=self.request.user, channel=NotificationChannel.IN_APP
        ).select_related("ride")


class PushTokenView(APIView):
    """POST /api/v1/users/me/push-token/ — register (or clear, with an empty string) this device's FCM token."""

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        serializer = PushTokenSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        request.user.push_token = serializer.validated_data["push_token"]
        request.user.save(update_fields=["push_token"])
        return Response(status=status.HTTP_204_NO_CONTENT)


class NotificationPreferencesView(APIView):
    """
    GET  /api/v1/users/me/notification-preferences/
    PATCH /api/v1/users/me/notification-preferences/ — partial update, any subset of the three channels.
    """

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        user = request.user
        return Response(
            {"notify_email": user.notify_email, "notify_sms": user.notify_sms, "notify_push": user.notify_push}
        )

    def patch(self, request):
        serializer = NotificationPreferencesSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        fields = list(serializer.validated_data.keys())
        for field, value in serializer.validated_data.items():
            setattr(request.user, field, value)
        if fields:
            request.user.save(update_fields=fields)
        return Response(
            {
                "notify_email": request.user.notify_email,
                "notify_sms": request.user.notify_sms,
                "notify_push": request.user.notify_push,
            }
        )
