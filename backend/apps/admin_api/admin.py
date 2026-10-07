from django.contrib import admin

from apps.admin_api.models import AuditLog


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    """
    Registered fully read-only. An audit log an administrator can edit or
    delete through the admin UI is not an audit log — the same reasoning
    behind the model having no update path in the service layer.
    """

    list_display = ["created_at", "actor_email", "action", "target_type", "target_id", "reason"]
    list_filter = ["action", "target_type"]
    search_fields = ["actor_email", "target_id", "reason"]
    readonly_fields = ["id", "actor", "actor_email", "action", "target_type", "target_id", "reason", "changes", "created_at", "updated_at"]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
