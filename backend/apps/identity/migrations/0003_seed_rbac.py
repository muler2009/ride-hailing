from django.db import migrations

PERMISSIONS = [
    ("ride.request", "Create a new ride request."),
    ("ride.cancel", "Cancel a ride the user is a party to."),
    ("ride.accept", "Accept an offered ride (driver)."),
    ("ride.start", "Start a trip (driver)."),
    ("ride.complete", "Complete a trip (driver)."),
    ("driver.approve", "Approve a driver application."),
    ("driver.suspend", "Suspend a driver account."),
    ("payment.refund", "Issue a refund."),
    ("pricing.manage", "Create or modify pricing rules."),
    ("user.manage", "Manage user accounts."),
]

ROLE_PERMISSIONS = {
    "Rider": ["ride.request", "ride.cancel"],
    "Driver": ["ride.accept", "ride.start", "ride.complete", "ride.cancel"],
    "Fleet Manager": [],
    "Support Agent": [],
    "Operations Manager": ["driver.approve", "driver.suspend", "pricing.manage"],
    "Finance Admin": ["payment.refund", "pricing.manage"],
    "Super Admin": None,  # all permissions
}

ROLE_DESCRIPTIONS = {
    "Rider": "Registered passenger who requests and pays for rides.",
    "Driver": "Registered, KYC-approved driver who fulfills ride requests.",
    "Fleet Manager": "Manages a roster of drivers and vehicles.",
    "Support Agent": "Handles support tickets and disputes.",
    "Operations Manager": "Manages service areas, pricing, and driver approvals.",
    "Finance Admin": "Manages payments, refunds, and payouts.",
    "Super Admin": "Unrestricted administrative access.",
}


def seed_rbac(apps, schema_editor):
    Permission = apps.get_model("identity", "Permission")
    Role = apps.get_model("identity", "Role")
    RolePermission = apps.get_model("identity", "RolePermission")

    codename_to_permission = {}
    for codename, description in PERMISSIONS:
        perm, _ = Permission.objects.update_or_create(
            codename=codename, defaults={"description": description}
        )
        codename_to_permission[codename] = perm

    all_permissions = list(codename_to_permission.values())

    for role_name, codenames in ROLE_PERMISSIONS.items():
        role, _ = Role.objects.update_or_create(
            name=role_name, defaults={"description": ROLE_DESCRIPTIONS.get(role_name, "")}
        )
        grant = all_permissions if codenames is None else [
            codename_to_permission[c] for c in codenames
        ]
        for perm in grant:
            RolePermission.objects.get_or_create(role=role, permission=perm)


def unseed_rbac(apps, schema_editor):
    Role = apps.get_model("identity", "Role")
    Permission = apps.get_model("identity", "Permission")
    Role.objects.filter(name__in=ROLE_PERMISSIONS.keys()).delete()
    Permission.objects.filter(codename__in=[c for c, _ in PERMISSIONS]).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("identity", "0002_initial"),
    ]

    operations = [
        migrations.RunPython(seed_rbac, unseed_rbac),
    ]
