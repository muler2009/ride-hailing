from django.db import transaction

from apps.identity import keycloak_client
from apps.identity.services import assign_role
from apps.users.models import User


@transaction.atomic
def register_user(
    email: str,
    password: str,
    phone_number: str = "",
    first_name: str = "",
    last_name: str = "",
    role: str = "Rider",
) -> User:
    """
    Registers a new user in Keycloak (the credential store) and mirrors a
    local profile row, then assigns the initial role both locally (so
    permissions are usable immediately, without waiting for a fresh token)
    and in Keycloak (so it's present in every subsequent issued token).

    Keycloak user creation happens first and is NOT rolled back if the
    local steps fail after it — Keycloak has no concept of our DB
    transaction. A failure here leaves an orphaned-but-harmless Keycloak
    user with no local counterpart; the next login attempt for that email
    will fail cleanly (no local user, no role) rather than silently
    succeeding half-configured. Operationally, reconcile such cases by
    re-running registration or deleting the orphaned Keycloak user.
    """
    keycloak_id = keycloak_client.create_keycloak_user(
        email=email, password=password, first_name=first_name, last_name=last_name
    )

    try:
        keycloak_client.assign_realm_role(keycloak_id, role)
    except keycloak_client.KeycloakError:
        # Realm role missing/misconfigured in Keycloak shouldn't block
        # registration outright — the local role assignment below still
        # grants working permissions; the token just won't carry the
        # Keycloak-side role claim until an admin fixes the realm config.
        pass

    user = User.objects.create(
        keycloak_id=keycloak_id,
        email=email,
        phone_number=phone_number or None,
        first_name=first_name,
        last_name=last_name,
    )
    user.set_unusable_password()
    user.save(update_fields=["password"])

    assign_role(user, role)
    return user
