from django.core.management.base import BaseCommand
from django.db import transaction

from apps.identity.models import Permission, Role, RolePermission

# Representative permission set from the SRS (Section 7.2). More are added
# as later domains (dispatch, payments, pricing, ...) come online; this
# command is safe to re-run at any point.
PERMISSIONS = [
    ("ride.request", "Create a new ride request."),
    ("ride.cancel", "Cancel a ride the user is a party to."),
    ("ride.accept", "Accept an offered ride (driver)."),
    ("ride.start", "Start a trip (driver)."),
    ("ride.complete", "Complete a trip (driver)."),
    ("ride.assign", "Manually assign a driver to a ride (Phase 3 dispatch stand-in)."),
    ("ride.manage", "View and monitor any ride."),
    ("driver.approve", "Approve a driver application."),
    ("driver.suspend", "Suspend a driver account."),
    ("payment.refund", "Issue a refund."),
    ("pricing.manage", "Create or modify pricing rules."),
    ("payout.manage", "Review and process driver payout requests."),
    ("user.manage", "Manage user accounts."),
]

# Role -> list of permission codenames. Super Admin gets everything,
# computed rather than listed by hand so newly added permissions are
# automatically included.
ROLE_PERMISSIONS = {
    "Rider": ["ride.request", "ride.cancel"],
    "Driver": ["ride.accept", "ride.start", "ride.complete", "ride.cancel"],
    "Fleet Manager": [],
    "Support Agent": ["ride.manage"],
    "Operations Manager": ["driver.approve", "driver.suspend", "pricing.manage", "ride.assign", "ride.manage"],
    "Finance Admin": ["payment.refund", "pricing.manage", "payout.manage"],
    "Super Admin": None,  # sentinel: all permissions
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


class Command(BaseCommand):
    help = "Seed default RBAC permissions and roles defined in the SRS."

    @transaction.atomic
    def handle(self, *args, **options):
        codename_to_permission = {}
        for codename, description in PERMISSIONS:
            perm, created = Permission.objects.update_or_create(
                codename=codename, defaults={"description": description}
            )
            codename_to_permission[codename] = perm
            self.stdout.write(f"{'Created' if created else 'Ensured'} permission: {codename}")

        all_permissions = list(codename_to_permission.values())

        for role_name, codenames in ROLE_PERMISSIONS.items():
            role, created = Role.objects.update_or_create(
                name=role_name,
                defaults={"description": ROLE_DESCRIPTIONS.get(role_name, "")},
            )
            self.stdout.write(f"{'Created' if created else 'Ensured'} role: {role_name}")

            grant = all_permissions if codenames is None else [
                codename_to_permission[c] for c in codenames
            ]
            for perm in grant:
                RolePermission.objects.get_or_create(role=role, permission=perm)

        self.stdout.write(self.style.SUCCESS("RBAC seed complete."))
