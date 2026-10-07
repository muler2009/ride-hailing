"""
Thin wrapper around Keycloak's OIDC and Admin REST APIs.

Deliberately implemented with plain `requests` calls rather than a
higher-level SDK: Keycloak's endpoints are stable, well-documented REST
APIs, and keeping this thin makes it easy to mock in tests without
depending on a third-party client library's internal behavior.

Requires a Keycloak realm pre-configured with:
  - A confidential client (KEYCLOAK_CLIENT_ID / KEYCLOAK_CLIENT_SECRET)
    with "Direct Access Grants" enabled (Resource Owner Password
    Credentials flow, used by our /auth/login/ proxy).
  - Realm roles matching our local Role names (Rider, Driver, ...) so a
    role assigned at registration shows up in the issued token's
    `realm_access.roles` claim.
  - An admin-capable service account (or the master-realm admin user) for
    the Admin API calls used at registration time.

See README.md "Keycloak setup" for a docker-compose snippet and the exact
realm configuration steps.
"""
from functools import lru_cache

import jwt
import requests
from django.conf import settings
from jwt import PyJWKClient


class KeycloakError(Exception):
    """Raised for any failed call to Keycloak (network, auth, or API error)."""


def _realm_url() -> str:
    return f"{settings.KEYCLOAK_SERVER_URL.rstrip('/')}/realms/{settings.KEYCLOAK_REALM}"


def _token_endpoint() -> str:
    return f"{_realm_url()}/protocol/openid-connect/token"


def _jwks_uri() -> str:
    return f"{_realm_url()}/protocol/openid-connect/certs"


def issuer() -> str:
    return _realm_url()


@lru_cache(maxsize=1)
def _jwks_client() -> PyJWKClient:
    return PyJWKClient(_jwks_uri())


def clear_jwks_cache() -> None:
    """Used by tests, and useful operationally after a Keycloak key rotation."""
    _jwks_client.cache_clear()


def verify_access_token(token: str) -> dict:
    """
    Verifies a Keycloak-issued access token's signature, issuer, and
    expiry, and returns its claims. Raises KeycloakError on any failure —
    callers should treat that uniformly as "unauthenticated", never leak
    the underlying reason to the client.
    """
    try:
        signing_key = _jwks_client().get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            issuer=issuer(),
            options={"verify_aud": False},  # Keycloak access tokens carry azp, not a single aud
        )
    except jwt.PyJWTError as exc:
        raise KeycloakError(f"Token verification failed: {exc}") from exc
    return claims


def request_token(username: str, password: str) -> dict:
    """
    Resource Owner Password Credentials grant — lets our /auth/login/
    endpoint keep the same request shape (email + password) for clients
    while Keycloak issues and owns the actual tokens.
    """
    response = requests.post(
        _token_endpoint(),
        data={
            "grant_type": "password",
            "client_id": settings.KEYCLOAK_CLIENT_ID,
            "client_secret": settings.KEYCLOAK_CLIENT_SECRET,
            "username": username,
            "password": password,
            "scope": "openid",
        },
        timeout=10,
    )
    if response.status_code != 200:
        raise KeycloakError(f"Login failed: {response.status_code} {response.text}")
    return response.json()


def refresh_access_token(refresh_token: str) -> dict:
    response = requests.post(
        _token_endpoint(),
        data={
            "grant_type": "refresh_token",
            "client_id": settings.KEYCLOAK_CLIENT_ID,
            "client_secret": settings.KEYCLOAK_CLIENT_SECRET,
            "refresh_token": refresh_token,
        },
        timeout=10,
    )
    if response.status_code != 200:
        raise KeycloakError(f"Token refresh failed: {response.status_code} {response.text}")
    return response.json()


# ---------------------------------------------------------------------------
# Admin API — user provisioning at registration time.
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def _admin_realm_url() -> str:
    return f"{settings.KEYCLOAK_SERVER_URL.rstrip('/')}/admin/realms/{settings.KEYCLOAK_REALM}"


def clear_admin_token_cache() -> None:
    _get_admin_token.cache_clear()


@lru_cache(maxsize=1)
def _get_admin_token() -> str:
    """
    Obtains a service-account token for the Admin API using the
    admin-cli client against the master realm. Cached for the process
    lifetime; Keycloak access tokens are short-lived, so in a long-running
    process you'd want a TTL-aware cache instead — fine for now given
    admin-API calls only happen at registration.
    """
    response = requests.post(
        f"{settings.KEYCLOAK_SERVER_URL.rstrip('/')}/realms/master/protocol/openid-connect/token",
        data={
            "grant_type": "password",
            "client_id": settings.KEYCLOAK_ADMIN_CLIENT_ID,
            "username": settings.KEYCLOAK_ADMIN_USERNAME,
            "password": settings.KEYCLOAK_ADMIN_PASSWORD,
        },
        timeout=10,
    )
    if response.status_code != 200:
        raise KeycloakError(f"Admin auth failed: {response.status_code} {response.text}")
    return response.json()["access_token"]


def _admin_headers() -> dict:
    return {"Authorization": f"Bearer {_get_admin_token()}"}


def create_keycloak_user(
    *, email: str, password: str, first_name: str = "", last_name: str = ""
) -> str:
    """Creates an enabled, email-verified user in Keycloak and returns its Keycloak user id."""
    response = requests.post(
        f"{_admin_realm_url()}/users",
        headers=_admin_headers(),
        json={
            "email": email,
            "username": email,
            "firstName": first_name,
            "lastName": last_name,
            "enabled": True,
            "emailVerified": True,
            "credentials": [{"type": "password", "value": password, "temporary": False}],
        },
        timeout=10,
    )
    if response.status_code == 409:
        raise KeycloakError("A Keycloak user with this email already exists.")
    if response.status_code != 201:
        raise KeycloakError(f"User creation failed: {response.status_code} {response.text}")

    location = response.headers["Location"]
    return location.rstrip("/").rsplit("/", 1)[-1]


def assign_realm_role(keycloak_user_id: str, role_name: str) -> None:
    """
    Assigns a realm role to a Keycloak user. The role must already exist in
    the realm (see README "Keycloak setup") — this does not create roles,
    it only maps an existing one, matching the platform's local Role names.
    """
    role_response = requests.get(
        f"{_admin_realm_url()}/roles/{role_name}", headers=_admin_headers(), timeout=10
    )
    if role_response.status_code != 200:
        raise KeycloakError(
            f"Realm role '{role_name}' not found in Keycloak — create it first: "
            f"{role_response.status_code} {role_response.text}"
        )
    role_repr = role_response.json()

    assign_response = requests.post(
        f"{_admin_realm_url()}/users/{keycloak_user_id}/role-mappings/realm",
        headers=_admin_headers(),
        json=[role_repr],
        timeout=10,
    )
    if assign_response.status_code != 204:
        raise KeycloakError(
            f"Role assignment failed: {assign_response.status_code} {assign_response.text}"
        )
