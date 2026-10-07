from django.contrib import admin

from apps.identity.models import Permission, Role, RolePermission, UserRole


class RolePermissionInline(admin.TabularInline):
    model = RolePermission
    extra = 1


@admin.register(Role)
class RoleAdmin(admin.ModelAdmin):
    list_display = ["name", "description"]
    search_fields = ["name"]
    inlines = [RolePermissionInline]


@admin.register(Permission)
class PermissionAdmin(admin.ModelAdmin):
    list_display = ["codename", "description"]
    search_fields = ["codename"]


@admin.register(UserRole)
class UserRoleAdmin(admin.ModelAdmin):
    list_display = ["user", "role", "assigned_by", "created_at"]
    autocomplete_fields = ["user", "role", "assigned_by"]
