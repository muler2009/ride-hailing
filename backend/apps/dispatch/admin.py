from django.contrib import admin

from apps.dispatch.models import DispatchOffer


@admin.register(DispatchOffer)
class DispatchOfferAdmin(admin.ModelAdmin):
    list_display = ["ride", "driver", "status", "distance_km", "expires_at", "responded_at"]
    list_filter = ["status"]
    autocomplete_fields = ["ride", "driver"]
    readonly_fields = ["id", "created_at", "updated_at"]
