from unittest import mock

from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from apps.identity import keycloak_client
from apps.identity.keycloak_test_utils import FakeJWKSClient, make_access_token
from apps.identity.services import assign_role
from apps.users.models import User


def _auth_header(token: str) -> str:
    return f"Bearer {token}"


class RegistrationTests(APITestCase):
    """
    Registration now provisions the user in Keycloak first (mocked here —
    no live Keycloak server is available in this environment) and mirrors
    a local profile row. See apps/users/services.py:register_user.
    """

    @mock.patch("apps.users.services.keycloak_client.assign_realm_role")
    @mock.patch("apps.users.services.keycloak_client.create_keycloak_user")
    def test_register_creates_user_with_default_rider_role(self, mock_create, mock_assign):
        mock_create.return_value = "kc-generated-id-1"

        url = reverse("register")
        payload = {
            "email": "rider@example.com",
            "password": "StrongPass123!",
            "first_name": "Ada",
            "last_name": "Lovelace",
        }
        response = self.client.post(url, payload, format="json")

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["email"], "rider@example.com")
        self.assertIn("Rider", response.data["roles"])
        self.assertIn("ride.request", response.data["permissions"])

        mock_create.assert_called_once_with(
            email="rider@example.com", password="StrongPass123!", first_name="Ada", last_name="Lovelace"
        )
        mock_assign.assert_called_once_with("kc-generated-id-1", "Rider")

        user = User.objects.get(email="rider@example.com")
        self.assertEqual(user.keycloak_id, "kc-generated-id-1")
        self.assertFalse(user.has_usable_password())

    @mock.patch("apps.users.services.keycloak_client.assign_realm_role")
    @mock.patch("apps.users.services.keycloak_client.create_keycloak_user")
    def test_register_with_driver_role(self, mock_create, mock_assign):
        mock_create.return_value = "kc-generated-id-2"

        url = reverse("register")
        payload = {"email": "driver@example.com", "password": "StrongPass123!", "role": "Driver"}
        response = self.client.post(url, payload, format="json")

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertIn("Driver", response.data["roles"])
        self.assertIn("ride.accept", response.data["permissions"])

    def test_duplicate_email_is_rejected_before_calling_keycloak(self):
        User.objects.create(email="taken@example.com", keycloak_id="kc-existing")

        url = reverse("register")
        with mock.patch("apps.users.services.keycloak_client.create_keycloak_user") as mock_create:
            response = self.client.post(
                url, {"email": "taken@example.com", "password": "StrongPass123!"}, format="json"
            )
            mock_create.assert_not_called()

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("error", response.data)

    def test_weak_password_is_rejected(self):
        url = reverse("register")
        response = self.client.post(
            url, {"email": "weak@example.com", "password": "123"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @mock.patch("apps.users.services.keycloak_client.create_keycloak_user")
    def test_keycloak_conflict_surfaces_as_error(self, mock_create):
        mock_create.side_effect = keycloak_client.KeycloakError("A Keycloak user with this email already exists.")

        url = reverse("register")
        with self.assertRaises(keycloak_client.KeycloakError):
            self.client.post(
                url, {"email": "conflict@example.com", "password": "StrongPass123!"}, format="json"
            )


class LoginTests(APITestCase):
    """
    Login proxies to Keycloak's token endpoint (mocked) and then verifies
    the returned access token using the real verification code path
    (self-signed token + mocked JWKS lookup — see KeycloakTokenVerificationTests
    in apps/identity/tests.py for lower-level coverage of that path).
    """

    def setUp(self):
        keycloak_client.clear_jwks_cache()
        patcher = mock.patch.object(keycloak_client, "_jwks_client", return_value=FakeJWKSClient())
        self.addCleanup(patcher.stop)
        patcher.start()

    @mock.patch("apps.users.views.request_token")
    def test_login_with_correct_credentials_returns_tokens(self, mock_request_token):
        access = make_access_token(sub="kc-login-1", email="login@example.com", realm_roles=["Rider"])
        mock_request_token.return_value = {"access_token": access, "refresh_token": "fake-refresh-token"}

        url = reverse("login")
        response = self.client.post(
            url, {"email": "login@example.com", "password": "StrongPass123!"}, format="json"
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["access"], access)
        self.assertEqual(response.data["refresh"], "fake-refresh-token")
        self.assertIn("Rider", response.data["user"]["roles"])

        # JIT-provisioned by the login flow itself.
        self.assertTrue(User.objects.filter(email="login@example.com", keycloak_id="kc-login-1").exists())

    @mock.patch("apps.users.views.request_token")
    def test_login_with_wrong_password_is_rejected(self, mock_request_token):
        mock_request_token.side_effect = keycloak_client.KeycloakError("Login failed: 401")

        url = reverse("login")
        response = self.client.post(
            url, {"email": "login@example.com", "password": "WrongPass!"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    @mock.patch("apps.users.views.request_token")
    def test_suspended_user_cannot_login(self, mock_request_token):
        access = make_access_token(sub="kc-suspended-1", email="suspended@example.com")
        mock_request_token.return_value = {"access_token": access, "refresh_token": "r"}

        # Pre-provision and suspend locally — Keycloak has no notion of our
        # platform-level suspension flag, so this check has to happen here.
        user = User.objects.create(email="suspended@example.com", keycloak_id="kc-suspended-1")
        user.suspend(reason="fraud review")

        url = reverse("login")
        response = self.client.post(
            url, {"email": "suspended@example.com", "password": "StrongPass123!"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)


class TokenRefreshTests(APITestCase):
    @mock.patch("apps.users.views.refresh_access_token")
    def test_refresh_returns_new_access_token(self, mock_refresh):
        mock_refresh.return_value = {"access_token": "new-access", "refresh_token": "new-refresh"}

        response = self.client.post(reverse("token_refresh"), {"refresh": "old-refresh"}, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["access"], "new-access")

    @mock.patch("apps.users.views.refresh_access_token")
    def test_invalid_refresh_token_is_rejected(self, mock_refresh):
        mock_refresh.side_effect = keycloak_client.KeycloakError("invalid_grant")

        response = self.client.post(reverse("token_refresh"), {"refresh": "bad"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)


class PermissionGatedEndpointTests(APITestCase):
    """
    Exercises the Phase 1 acceptance criterion end-to-end against the new
    auth backend: mint a self-signed token shaped like a real Keycloak
    token, present it as a Bearer credential, and confirm
    KeycloakAuthentication + the local RBAC permission check behave
    correctly for both an allowed and a denied caller.
    """

    def setUp(self):
        keycloak_client.clear_jwks_cache()
        patcher = mock.patch.object(keycloak_client, "_jwks_client", return_value=FakeJWKSClient())
        self.addCleanup(patcher.stop)
        patcher.start()

        self.rider = User.objects.create(email="rider2@example.com", keycloak_id="kc-rider-2")
        assign_role(self.rider, "Rider")

        self.admin = User.objects.create(email="admin@example.com", keycloak_id="kc-admin-1")
        assign_role(self.admin, "Super Admin")

    def test_rider_is_denied_access_to_admin_user_list(self):
        token = make_access_token(sub="kc-rider-2", email="rider2@example.com")
        response = self.client.get(reverse("user_list"), HTTP_AUTHORIZATION=_auth_header(token))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_super_admin_is_allowed_access_to_admin_user_list(self):
        token = make_access_token(sub="kc-admin-1", email="admin@example.com")
        response = self.client.get(reverse("user_list"), HTTP_AUTHORIZATION=_auth_header(token))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("results", response.data)

    def test_unauthenticated_request_is_rejected(self):
        response = self.client.get(reverse("user_list"))
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_expired_token_is_rejected(self):
        token = make_access_token(sub="kc-rider-2", email="rider2@example.com", expired=True)
        response = self.client.get(reverse("user_me"), HTTP_AUTHORIZATION=_auth_header(token))
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_me_endpoint_returns_own_profile(self):
        token = make_access_token(sub="kc-rider-2", email="rider2@example.com")
        response = self.client.get(reverse("user_me"), HTTP_AUTHORIZATION=_auth_header(token))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["email"], "rider2@example.com")

    def test_suspended_user_is_rejected_even_with_a_valid_token(self):
        self.rider.suspend(reason="fraud review")
        token = make_access_token(sub="kc-rider-2", email="rider2@example.com")
        response = self.client.get(reverse("user_me"), HTTP_AUTHORIZATION=_auth_header(token))
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
