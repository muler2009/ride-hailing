from unittest import mock

from django.test import TestCase

from apps.identity import keycloak_client
from apps.identity.keycloak_test_utils import FakeJWKSClient, make_access_token
from apps.identity.models import Permission, Role
from apps.identity.services import (
    assign_role,
    get_user_permission_codenames,
    invalidate_user_permission_cache,
    sync_user_from_keycloak_claims,
    user_has_permission,
)
from apps.users.models import User


class RbacServiceTests(TestCase):
    def setUp(self):
        self.user = User.objects.create(email="rbac@example.com", keycloak_id="kc-rbac-1")
        self.perm = Permission.objects.create(
            codename="test.custom_permission", description="A test-only permission."
        )
        self.role = Role.objects.create(name="Test Role")
        self.role.permissions.add(self.perm)

    def test_user_without_role_has_no_permissions(self):
        self.assertEqual(get_user_permission_codenames(self.user), set())
        self.assertFalse(user_has_permission(self.user, "test.custom_permission"))

    def test_assigning_role_grants_its_permissions(self):
        assign_role(self.user, "Test Role")
        self.assertTrue(user_has_permission(self.user, "test.custom_permission"))
        self.assertFalse(user_has_permission(self.user, "nonexistent.permission"))

    def test_permission_cache_is_invalidated_on_role_change(self):
        assign_role(self.user, "Test Role")
        self.assertTrue(user_has_permission(self.user, "test.custom_permission"))

        self.user.user_roles.all().delete()
        invalidate_user_permission_cache(self.user.id)

        self.assertFalse(user_has_permission(self.user, "test.custom_permission"))

    def test_anonymous_user_has_no_permissions(self):
        self.assertEqual(get_user_permission_codenames(None), set())


class KeycloakTokenVerificationTests(TestCase):
    """
    Exercises the real verification logic (signature, issuer, expiry) with
    a self-signed token, standing in for a token actually issued by
    Keycloak. Only the network call to fetch the JWKS is replaced.
    """

    def setUp(self):
        keycloak_client.clear_jwks_cache()
        patcher = mock.patch.object(
            keycloak_client, "_jwks_client", return_value=FakeJWKSClient()
        )
        self.addCleanup(patcher.stop)
        patcher.start()

    def test_valid_token_is_verified(self):
        token = make_access_token(sub="kc-1", email="rider@example.com")
        claims = keycloak_client.verify_access_token(token)
        self.assertEqual(claims["sub"], "kc-1")
        self.assertEqual(claims["email"], "rider@example.com")

    def test_expired_token_is_rejected(self):
        token = make_access_token(sub="kc-1", email="rider@example.com", expired=True)
        with self.assertRaises(keycloak_client.KeycloakError):
            keycloak_client.verify_access_token(token)

    def test_wrong_issuer_is_rejected(self):
        token = make_access_token(sub="kc-1", email="rider@example.com", wrong_issuer=True)
        with self.assertRaises(keycloak_client.KeycloakError):
            keycloak_client.verify_access_token(token)

    def test_tampered_token_is_rejected(self):
        token = make_access_token(sub="kc-1", email="rider@example.com")
        tampered = token[:-4] + ("A" * 4)
        with self.assertRaises(keycloak_client.KeycloakError):
            keycloak_client.verify_access_token(tampered)


class SyncUserFromKeycloakClaimsTests(TestCase):
    def setUp(self):
        Role.objects.get_or_create(name="Rider")
        Role.objects.get_or_create(name="Driver")

    def test_creates_local_user_on_first_sync(self):
        claims = {
            "sub": "kc-new-1",
            "email": "new@example.com",
            "given_name": "Ada",
            "family_name": "Lovelace",
            "realm_access": {"roles": ["Rider"]},
        }
        user = sync_user_from_keycloak_claims(claims)

        self.assertEqual(user.email, "new@example.com")
        self.assertEqual(user.first_name, "Ada")
        self.assertFalse(user.has_usable_password())
        self.assertIn("Rider", user.user_roles.values_list("role__name", flat=True))

    def test_sync_is_idempotent_and_additive_only(self):
        claims = {"sub": "kc-new-2", "email": "idempotent@example.com", "realm_access": {"roles": ["Rider"]}}
        user1 = sync_user_from_keycloak_claims(claims)
        user1.user_roles.filter(role__name="Rider").delete()
        invalidate_user_permission_cache(user1.id)

        claims_with_extra_role = {
            "sub": "kc-new-2",
            "email": "idempotent@example.com",
            "realm_access": {"roles": ["Rider", "Driver"]},
        }
        user2 = sync_user_from_keycloak_claims(claims_with_extra_role)

        self.assertEqual(user1.id, user2.id)
        role_names = set(user2.user_roles.values_list("role__name", flat=True))
        self.assertEqual(role_names, {"Rider", "Driver"})

    def test_unknown_realm_role_is_ignored(self):
        claims = {
            "sub": "kc-new-3",
            "email": "unknown-role@example.com",
            "realm_access": {"roles": ["SomeRoleNotInLocalRbac"]},
        }
        user = sync_user_from_keycloak_claims(claims)
        self.assertEqual(user.user_roles.count(), 0)

    def test_missing_required_claims_raises(self):
        with self.assertRaises(ValueError):
            sync_user_from_keycloak_claims({"email": "no-sub@example.com"})
