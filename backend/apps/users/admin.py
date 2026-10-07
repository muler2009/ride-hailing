from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin

from apps.users.models import User


@admin.register(User)
class UserAdmin(DjangoUserAdmin):
    ordering = ["-created_at"]
    list_display = ["email", "keycloak_id", "phone_number", "is_active", "is_suspended", "is_staff", "created_at"]
    search_fields = ["email", "keycloak_id", "phone_number", "first_name", "last_name"]
    readonly_fields = ["id", "keycloak_id", "created_at", "updated_at"]
    fieldsets = (
        (None, {"fields": ("email", "keycloak_id", "password")}),
        ("Personal info", {"fields": ("first_name", "last_name", "phone_number")}),
        (
            "Status",
            {
                "fields": (
                    "is_active",
                    "is_staff",
                    "is_superuser",
                    "is_suspended",
                    "suspended_at",
                    "suspended_reason",
                )
            },
        ),
        ("Important dates", {"fields": ("last_login", "created_at", "updated_at")}),
    )
    add_fieldsets = (
        (
            None,
            {
                "classes": ("wide",),
                "fields": ("email", "password1", "password2"),
                "description": (
                    "Only for creating local Django-admin staff accounts. "
                    "Regular platform users register through the API, which "
                    "provisions them in Keycloak — do not create rider/driver "
                    "accounts here."
                ),
            },
        ),
    )
