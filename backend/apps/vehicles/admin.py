from django.contrib import admin

from apps.vehicles.models import Vehicle, VehicleDocument, VehicleType


class VehicleDocumentInline(admin.TabularInline):
    model = VehicleDocument
    extra = 0
    readonly_fields = ["created_at"]


@admin.register(VehicleType)
class VehicleTypeAdmin(admin.ModelAdmin):
    list_display = ["name", "passenger_capacity", "is_active"]
    search_fields = ["name"]


@admin.register(Vehicle)
class VehicleAdmin(admin.ModelAdmin):
    list_display = ["license_plate", "driver", "vehicle_type", "make", "model", "year", "is_active"]
    list_filter = ["vehicle_type", "is_active"]
    search_fields = ["license_plate", "driver__user__email", "make", "model"]
    autocomplete_fields = ["driver", "vehicle_type"]
    readonly_fields = ["id", "created_at", "updated_at"]
    inlines = [VehicleDocumentInline]


@admin.register(VehicleDocument)
class VehicleDocumentAdmin(admin.ModelAdmin):
    list_display = ["vehicle", "document_type", "status", "created_at"]
    list_filter = ["document_type", "status"]
    autocomplete_fields = ["vehicle", "reviewed_by"]
