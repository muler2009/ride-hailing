from django.contrib import admin

from apps.earnings.models import DriverEarning, Payout, Wallet, WalletLedgerEntry


class WalletLedgerEntryInline(admin.TabularInline):
    model = WalletLedgerEntry
    extra = 0
    readonly_fields = ["entry_type", "amount", "description", "ride", "payout", "idempotency_key", "created_at"]
    can_delete = False
    ordering = ["created_at"]


@admin.register(Wallet)
class WalletAdmin(admin.ModelAdmin):
    list_display = ["driver", "currency"]
    search_fields = ["driver__user__email"]
    autocomplete_fields = ["driver"]
    readonly_fields = ["id", "created_at", "updated_at"]
    inlines = [WalletLedgerEntryInline]


@admin.register(DriverEarning)
class DriverEarningAdmin(admin.ModelAdmin):
    list_display = ["ride", "driver", "fare_amount", "commission_amount", "driver_earning_amount", "created_at"]
    autocomplete_fields = ["ride", "driver"]
    readonly_fields = ["id", "created_at", "updated_at"]


@admin.register(Payout)
class PayoutAdmin(admin.ModelAdmin):
    list_display = ["driver", "amount", "status", "processed_at", "created_at"]
    list_filter = ["status"]
    search_fields = ["id", "driver__user__email"]
    autocomplete_fields = ["driver", "processed_by"]
    readonly_fields = ["id", "created_at", "updated_at"]


@admin.register(WalletLedgerEntry)
class WalletLedgerEntryAdmin(admin.ModelAdmin):
    list_display = ["wallet", "entry_type", "amount", "created_at"]
    list_filter = ["entry_type"]
    autocomplete_fields = ["wallet", "ride", "payout"]
    readonly_fields = ["id", "idempotency_key", "created_at", "updated_at"]
