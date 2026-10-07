"""
Test support for simulating Keycloak-issued tokens without a running
Keycloak server. Generates a real RSA keypair and signs tokens with it,
so the actual verification code path (signature check via a JWKS-style
lookup, issuer/expiry validation) is exercised for real — only the
network call to fetch the JWKS is replaced.

NOT a test module itself (no TestCase here) — imported by test modules
in apps/identity and apps/users.
"""
import time

from cryptography.hazmat.primitives.asymmetric import rsa
from django.conf import settings

_PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_PUBLIC_KEY = _PRIVATE_KEY.public_key()


class FakeSigningKey:
    """Mimics the object PyJWKClient.get_signing_key_from_jwt() returns."""

    def __init__(self, key):
        self.key = key


class FakeJWKSClient:
    def get_signing_key_from_jwt(self, token):
        return FakeSigningKey(_PUBLIC_KEY)


def issuer() -> str:
    return f"{settings.KEYCLOAK_SERVER_URL.rstrip('/')}/realms/{settings.KEYCLOAK_REALM}"


def make_access_token(
    *,
    sub: str,
    email: str,
    realm_roles=None,
    given_name: str = "",
    family_name: str = "",
    expired: bool = False,
    wrong_issuer: bool = False,
) -> str:
    import jwt

    now = int(time.time())
    payload = {
        "sub": sub,
        "email": email,
        "given_name": given_name,
        "family_name": family_name,
        "iss": "https://wrong-issuer.example.com/realms/other" if wrong_issuer else issuer(),
        "iat": now,
        "exp": now - 10 if expired else now + 300,
        "realm_access": {"roles": realm_roles or []},
    }
    return jwt.encode(payload, _PRIVATE_KEY, algorithm="RS256")
