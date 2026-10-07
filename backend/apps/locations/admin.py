from django.contrib import admin

from apps.locations.models import DriverLocation


@admin.register(DriverLocation)
class DriverLocationAdmin(admin.ModelAdmin):
    list_display = ["driver", "ride", "latitude", "longitude", "recorded_at"]
    list_filter = ["recorded_at"]
    autocomplete_fields = ["driver", "ride"]
    readonly_fields = ["id", "created_at", "updated_at"]
