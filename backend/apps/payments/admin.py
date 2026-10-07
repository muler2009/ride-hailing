from django.contrib import admin

from apps.payments.models import Payment, PaymentTransaction


class PaymentTransactionInline(admin.TabularInline):
    model = PaymentTransaction
    extra = 0
    readonly_fields = ["transaction_type", "amount", "provider_reference", "idempotency_key", "initiated_by", "created_at"]
    can_delete = False
    ordering = ["created_at"]


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = ["ride", "method", "provider_name", "status", "currency", "amount", "created_at"]
    list_filter = ["method", "status", "provider_name"]
    search_fields = ["ride__id", "provider_reference", "idempotency_key"]
    autocomplete_fields = ["ride", "fare"]
    readonly_fields = ["id", "idempotency_key", "created_at", "updated_at"]
    inlines = [PaymentTransactionInline]


@admin.register(PaymentTransaction)
class PaymentTransactionAdmin(admin.ModelAdmin):
    list_display = ["payment", "transaction_type", "amount", "created_at"]
    list_filter = ["transaction_type"]
    autocomplete_fields = ["payment", "initiated_by"]
    readonly_fields = ["id", "idempotency_key", "created_at", "updated_at"]
