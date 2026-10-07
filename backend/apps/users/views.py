from rest_framework import generics, permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.common.pagination import StandardResultsPagination
from apps.identity.keycloak_client import (
    KeycloakError,
    refresh_access_token,
    request_token,
    verify_access_token,
)
from apps.identity.permissions import HasPermission
from apps.identity.services import sync_user_from_keycloak_claims
from apps.users.models import User
from apps.users.serializers import (
    LoginSerializer,
    RefreshSerializer,
    RegisterSerializer,
    UserSerializer,
)


class RegisterView(generics.CreateAPIView):
    """POST /api/v1/auth/register/ — public registration endpoint."""

    permission_classes = [permissions.AllowAny]
    serializer_class = RegisterSerializer

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        return Response(
            UserSerializer(user).data,
            status=status.HTTP_201_CREATED,
        )


class LoginView(APIView):
    """
    POST /api/v1/auth/login/ — exchanges email+password for a Keycloak
    token pair. The request/response shape is unchanged from the previous
    simplejwt-based implementation; only the token issuer changed.
    """

    permission_classes = [permissions.AllowAny]

    def post(self, request):
        serializer = LoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        email = serializer.validated_data["email"]
        password = serializer.validated_data["password"]

        try:
            token_response = request_token(email, password)
        except KeycloakError:
            return Response(
                {"error": {"code": "invalid_credentials", "message": "Invalid email or password.", "details": None}},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        access_token = token_response["access_token"]
        claims = verify_access_token(access_token)
        user = sync_user_from_keycloak_claims(claims)

        if user.is_suspended or not user.is_active:
            return Response(
                {"error": {"code": "account_suspended", "message": "This account has been suspended.", "details": None}},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        return Response(
            {
                "access": access_token,
                "refresh": token_response["refresh_token"],
                "user": {
                    "id": str(user.id),
                    "email": user.email,
                    "roles": sorted(user.user_roles.values_list("role__name", flat=True)),
                },
            },
            status=status.HTTP_200_OK,
        )


class TokenRefreshView(APIView):
    """POST /api/v1/auth/refresh/ — exchanges a refresh token for a new access token."""

    permission_classes = [permissions.AllowAny]

    def post(self, request):
        serializer = RefreshSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            token_response = refresh_access_token(serializer.validated_data["refresh"])
        except KeycloakError:
            return Response(
                {"error": {"code": "invalid_refresh_token", "message": "Refresh token is invalid or expired.", "details": None}},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        return Response(
            {
                "access": token_response["access_token"],
                "refresh": token_response.get("refresh_token", serializer.validated_data["refresh"]),
            }
        )


class MeView(generics.RetrieveAPIView):
    """GET /api/v1/users/me/ — the authenticated user's own profile."""

    permission_classes = [permissions.IsAuthenticated]
    serializer_class = UserSerializer

    def get_object(self):
        return self.request.user


class UserListView(generics.ListAPIView):
    """
    GET /api/v1/users/ — administrative user listing.

    Only a caller whose local roles grant `user.manage` (e.g. Super Admin)
    may list all users; anyone else gets 403. Unchanged by the Keycloak
    migration — authorization still runs entirely through the local RBAC
    tables, regardless of which identity provider authenticated the caller.
    """

    permission_classes = [permissions.IsAuthenticated, HasPermission]
    required_permission = "user.manage"
    serializer_class = UserSerializer
    pagination_class = StandardResultsPagination
    queryset = User.objects.all().order_by("-created_at")
