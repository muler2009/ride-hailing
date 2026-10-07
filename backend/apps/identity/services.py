from django.core.cache import cache

from apps.identity.models import Role, UserRole

CACHE_TTL_SECONDS = 60
CACHE_KEY_TEMPLATE = "user_permissions:{user_id}"


def get_user_permission_codenames(user) -> set[str]:
    """
    Returns the full set of permission codenames granted to a user through
    all of their assigned roles. This is the ONLY function in the codebase
    that should be used to answer "can this user do X" — permission.py's
    DRF permission classes call into this, and nothing else computes
    authorization independently.
    """
    if user is None or not getattr(user, "is_authenticated", False):
        return set()

    cache_key = CACHE_KEY_TEMPLATE.format(user_id=user.id)
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    codenames = set(
        UserRole.objects.filter(user_id=user.id)
        .values_list("role__permissions__codename", flat=True)
        .distinct()
    )
    codenames.discard(None)

    cache.set(cache_key, codenames, CACHE_TTL_SECONDS)
    return codenames


def user_has_permission(user, codename: str) -> bool:
    return codename in get_user_permission_codenames(user)


def invalidate_user_permission_cache(user_id) -> None:
    cache.delete(CACHE_KEY_TEMPLATE.format(user_id=user_id))


def assign_role(user, role_name: str, assigned_by=None) -> UserRole:
    role = Role.objects.get(name=role_name)
    user_role, _created = UserRole.objects.get_or_create(
        user=user, role=role, defaults={"assigned_by": assigned_by}
    )
    invalidate_user_permission_cache(user.id)
    return user_role


def sync_user_from_keycloak_claims(claims: dict):
    """
    Resolves a verified Keycloak token's claims to a local User, creating
    or updating it as needed (JIT provisioning), and additively syncs any
    realm roles present in the token that match a local Role name.

    This only ever *adds* local role assignments to mirror Keycloak state;
    it never removes one. De-provisioning (e.g. a role revoked in
    Keycloak) is handled by an explicit admin action locally
    (see apps/drivers/services.py's suspend/reject flows for the pattern),
    not by silently stripping permissions out from under an active
    session on every request.
    """
    from django.contrib.auth import get_user_model

    User = get_user_model()

    keycloak_id = claims.get("sub")
    email = claims.get("email")
    if not keycloak_id or not email:
        raise ValueError("Keycloak token is missing required claims (sub/email).")

    defaults = {
        "email": email,
        "first_name": claims.get("given_name") or "",
        "last_name": claims.get("family_name") or "",
    }

    user, created = User.objects.get_or_create(keycloak_id=keycloak_id, defaults=defaults)
    if created:
        user.set_unusable_password()
        user.save(update_fields=["password"])
    else:
        changed_fields = [
            field
            for field, value in defaults.items()
            if value and getattr(user, field) != value
        ]
        if changed_fields:
            for field in changed_fields:
                setattr(user, field, defaults[field])
            user.save(update_fields=changed_fields)

    realm_roles = set((claims.get("realm_access") or {}).get("roles", []))
    if realm_roles:
        known_role_names = set(
            Role.objects.filter(name__in=realm_roles).values_list("name", flat=True)
        )
        already_assigned = set(user.user_roles.values_list("role__name", flat=True))
        for role_name in known_role_names - already_assigned:
            assign_role(user, role_name)

    return user
