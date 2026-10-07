from django.db import migrations

NEW_PERMISSIONS = [
    ("payout.manage", "Review and process driver payout requests."),
]

NEW_GRANTS = {
    "Finance Admin": ["payout.manage"],
}


def seed_payout_permissions(apps, schema_editor):
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

    super_admin, _ = Role.objects.get_or_create(name="Super Admin")
    for perm in codename_to_permission.values():
        RolePermission.objects.get_or_create(role=super_admin, permission=perm)


def unseed_payout_permissions(apps, schema_editor):
    Permission = apps.get_model("identity", "Permission")
    Permission.objects.filter(codename__in=[c for c, _ in NEW_PERMISSIONS]).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("identity", "0004_seed_ride_permissions"),
    ]

    operations = [
        migrations.RunPython(seed_payout_permissions, unseed_payout_permissions),
    ]
