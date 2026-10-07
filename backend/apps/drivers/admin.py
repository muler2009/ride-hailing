from django.contrib import admin

from apps.drivers.models import Driver, DriverDocument


class DriverDocumentInline(admin.TabularInline):
    model = DriverDocument
    extra = 0
    readonly_fields = ["created_at"]


@admin.register(Driver)
class DriverAdmin(admin.ModelAdmin):
    list_display = ["user", "approval_status", "license_number", "license_expiry", "reviewed_at"]
    list_filter = ["approval_status"]
    search_fields = ["user__email", "license_number"]
    autocomplete_fields = ["user", "reviewed_by"]
    readonly_fields = ["id", "created_at", "updated_at"]
    inlines = [DriverDocumentInline]


@admin.register(DriverDocument)
class DriverDocumentAdmin(admin.ModelAdmin):
    list_display = ["driver", "document_type", "status", "created_at"]
    list_filter = ["document_type", "status"]
    autocomplete_fields = ["driver", "reviewed_by"]
