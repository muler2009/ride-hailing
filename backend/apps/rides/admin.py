from django.contrib import admin

from apps.rides.models import Ride, RideStatusHistory


class RideStatusHistoryInline(admin.TabularInline):
    model = RideStatusHistory
    extra = 0
    readonly_fields = ["from_status", "to_status", "changed_by", "note", "created_at"]
    can_delete = False
    ordering = ["created_at"]


@admin.register(Ride)
class RideAdmin(admin.ModelAdmin):
    list_display = ["id", "status", "rider_or_guest", "driver", "vehicle_type", "created_at"]
    list_filter = ["status", "vehicle_type"]
    search_fields = ["id", "rider__email", "guest_phone_number", "driver__user__email"]
    autocomplete_fields = ["rider", "driver", "vehicle_type"]
    readonly_fields = ["id", "tracking_token", "version", "created_at", "updated_at"]
    inlines = [RideStatusHistoryInline]

    def rider_or_guest(self, obj):
        return obj.rider.email if obj.rider_id else f"guest:{obj.guest_phone_number}"


@admin.register(RideStatusHistory)
class RideStatusHistoryAdmin(admin.ModelAdmin):
    list_display = ["ride", "from_status", "to_status", "changed_by", "created_at"]
    list_filter = ["to_status"]
    autocomplete_fields = ["ride", "changed_by"]
