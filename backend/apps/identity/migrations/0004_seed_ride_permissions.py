from django.db import migrations

NEW_PERMISSIONS = [
    ("ride.assign", "Manually assign a driver to a ride (Phase 3 dispatch stand-in)."),
    ("ride.manage", "View and monitor any ride."),
]

# Role name -> list of the new permission codenames to grant. Super Admin
# is handled separately below (it must receive every permission, including
# ones that didn't exist when 0003_seed_rbac ran).
NEW_GRANTS = {
    "Support Agent": ["ride.manage"],
    "Operations Manager": ["ride.assign", "ride.manage"],
}


def seed_ride_permissions(apps, schema_editor):
    Permission = apps.get_model("identity", "Permission")
    Role = apps.get_model("identity", "Role")
    RolePermission = apps.get_model("identity", "RolePermission")

    codename_to_permission = {}
    for codename, description in NEW_PERMISSIONS:
        perm, _ = Permission.objects.update_or_create(
            codename=codename, defaults={"description": description}
        )
        codename_to_permission[codename] = perm

    for role_name, codenames in NEW_GRANTS.items():
        role, _ = Role.objects.get_or_create(name=role_name)
        for codename in codenames:
            RolePermission.objects.get_or_create(role=role, permission=codename_to_permission[codename])

    # Super Admin gets every permission, including these new ones —
    # 0003_seed_rbac's "grant all" sentinel only applied to the permissions
    # that existed at the time it ran.
    super_admin, _ = Role.objects.get_or_create(name="Super Admin")
    for perm in codename_to_permission.values():
        RolePermission.objects.get_or_create(role=super_admin, permission=perm)


def unseed_ride_permissions(apps, schema_editor):
    Permission = apps.get_model("identity", "Permission")
    Permission.objects.filter(codename__in=[c for c, _ in NEW_PERMISSIONS]).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("identity", "0003_seed_rbac"),
    ]

    operations = [
        migrations.RunPython(seed_ride_permissions, unseed_ride_permissions),
    ]
