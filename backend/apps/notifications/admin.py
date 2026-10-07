from django.contrib import admin

from apps.notifications.models import Notification


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = ["recipient_or_guest", "channel", "event_type", "status", "created_at", "sent_at"]
    list_filter = ["channel", "event_type", "status"]
    search_fields = ["recipient_user__email", "guest_phone_number", "idempotency_key"]
    autocomplete_fields = ["recipient_user", "ride"]
    readonly_fields = ["id", "idempotency_key", "created_at", "updated_at"]

    def recipient_or_guest(self, obj):
        return obj.recipient_user.email if obj.recipient_user_id else f"guest:{obj.guest_phone_number}"
