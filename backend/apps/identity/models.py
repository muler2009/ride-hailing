from django.conf import settings
from django.db import models

from apps.common.models import BaseModel


class Permission(BaseModel):
    """
    A single, fine-grained capability — e.g. 'ride.request', 'driver.approve'.

    Authorization throughout the platform is checked against permission
    codenames, never against role names directly, so role-to-permission
    mappings can be reconfigured without touching application code.
    """

    codename = models.CharField(max_length=100, unique=True, db_index=True)
    description = models.CharField(max_length=255, blank=True)

    class Meta:
        db_table = "identity_permission"
        ordering = ["codename"]

    def __str__(self):
        return self.codename


class Role(BaseModel):
    """
    A named bundle of permissions. Ships with seven default roles
    (see apps/identity/fixtures / seed_rbac management command):
    Rider, Driver, Fleet Manager, Support Agent, Operations Manager,
    Finance Admin, Super Admin.
    """

    name = models.CharField(max_length=50, unique=True, db_index=True)
    description = models.CharField(max_length=255, blank=True)
    permissions = models.ManyToManyField(
        Permission, through="RolePermission", related_name="roles"
    )

    class Meta:
        db_table = "identity_role"
        ordering = ["name"]

    def __str__(self):
        return self.name


class RolePermission(BaseModel):
    """Explicit join table (rather than a bare M2M) so grants are auditable and timestamped."""

    role = models.ForeignKey(Role, on_delete=models.CASCADE, related_name="role_permissions")
    permission = models.ForeignKey(
        Permission, on_delete=models.CASCADE, related_name="permission_roles"
    )

    class Meta:
        db_table = "identity_role_permission"
        constraints = [
            models.UniqueConstraint(
                fields=["role", "permission"], name="uniq_role_permission"
            )
        ]

    def __str__(self):
        return f"{self.role.name} -> {self.permission.codename}"


class UserRole(BaseModel):
    """
    Assigns a Role to a User. A user may hold more than one role
    (e.g. a Driver who is also a Fleet Manager).
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="user_roles"
    )
    role = models.ForeignKey(Role, on_delete=models.CASCADE, related_name="role_users")
    assigned_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="roles_assigned",
    )

    class Meta:
        db_table = "identity_user_role"
        constraints = [
            models.UniqueConstraint(fields=["user", "role"], name="uniq_user_role")
        ]

    def __str__(self):
        return f"{self.user} -> {self.role.name}"
