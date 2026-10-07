from django.contrib import admin

from apps.analytics.models import DailyPlatformRollup, DailyVehicleTypeRollup


class DailyVehicleTypeRollupInline(admin.TabularInline):
    model = DailyVehicleTypeRollup
    fk_name = "rollup"
    extra = 0
    can_delete = False
    fields = ["vehicle_type", "ride_count", "gross_revenue", "platform_commission"]
    readonly_fields = fields

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(DailyPlatformRollup)
class DailyPlatformRollupAdmin(admin.ModelAdmin):
    """
    Read-only, like admin_api's AuditLog: this table is a derived
    projection recomputed from source data, not something an operator
    should ever hand-edit through the admin UI. To fix a wrong number,
    re-run the rollup (management command or task) for that date.
    """

    list_display = ["date", "rides_requested", "rides_completed", "gross_revenue", "platform_commission"]
    list_filter = ["date"]
    ordering = ["-date"]
    inlines = [DailyVehicleTypeRollupInline]
    readonly_fields = [f.name for f in DailyPlatformRollup._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
