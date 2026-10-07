from django.contrib import admin

from apps.pricing.models import Fare, FareLineItem, PricingRule


@admin.register(PricingRule)
class PricingRuleAdmin(admin.ModelAdmin):
    list_display = ["vehicle_type", "currency", "base_fare", "per_km_rate", "per_minute_rate", "surge_multiplier"]
    autocomplete_fields = ["vehicle_type"]
    readonly_fields = ["id", "created_at", "updated_at"]


class FareLineItemInline(admin.TabularInline):
    model = FareLineItem
    extra = 0
    readonly_fields = ["item_type", "label", "amount", "sequence"]
    can_delete = False
    ordering = ["sequence"]


@admin.register(Fare)
class FareAdmin(admin.ModelAdmin):
    list_display = ["ride", "fare_type", "currency", "total_amount", "distance_km", "duration_minutes", "created_at"]
    list_filter = ["fare_type"]
    search_fields = ["id", "ride__id"]
    autocomplete_fields = ["ride"]
    readonly_fields = ["id", "created_at", "updated_at"]
    inlines = [FareLineItemInline]
