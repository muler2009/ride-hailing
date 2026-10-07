from rest_framework import authentication, exceptions

from apps.identity.keycloak_client import KeycloakError, verify_access_token
from apps.identity.services import sync_user_from_keycloak_claims


class KeycloakAuthentication(authentication.BaseAuthentication):
    """
    Validates a Keycloak-issued Bearer access token and resolves it to a
    local User record, creating or updating that record on the fly
    (JIT provisioning) so the rest of the platform can keep working with a
    normal Django user object without needing a local copy of every
    Keycloak field.

    This is the ONLY place a raw Keycloak token is decoded. Authorization
    decisions still go entirely through apps/identity/services.py and the
    local Role/Permission tables — Keycloak is the identity provider, not
    the authorization engine.
    """

    keyword = b"bearer"

    def authenticate(self, request):
        auth_header = authentication.get_authorization_header(request)
        if not auth_header:
            return None

        parts = auth_header.split()
        if parts[0].lower() != self.keyword:
            return None
        if len(parts) != 2:
            raise exceptions.AuthenticationFailed("Malformed Authorization header.")

        token = parts[1].decode("utf-8")

        try:
            claims = verify_access_token(token)
        except KeycloakError as exc:
            raise exceptions.AuthenticationFailed("Invalid or expired token.") from exc

        user = sync_user_from_keycloak_claims(claims)

        if not user.is_active or user.is_suspended:
            raise exceptions.AuthenticationFailed("This account is inactive or suspended.")

        return (user, token)

    def authenticate_header(self, request):
        return 'Bearer realm="ride-hailing"'
