from django.urls import path

from apps.notifications.views import MyNotificationsListView, NotificationPreferencesView, PushTokenView

urlpatterns = [
    path("notifications/me/", MyNotificationsListView.as_view(), name="my_notifications"),
    path("users/me/push-token/", PushTokenView.as_view(), name="push_token"),
    path(
        "users/me/notification-preferences/",
        NotificationPreferencesView.as_view(),
        name="notification_preferences",
    ),
]
