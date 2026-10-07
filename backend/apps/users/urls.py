from django.urls import path

from apps.users.views import LoginView, MeView, RegisterView, TokenRefreshView, UserListView

urlpatterns = [
    path("auth/register/", RegisterView.as_view(), name="register"),
    path("auth/login/", LoginView.as_view(), name="login"),
    path("auth/refresh/", TokenRefreshView.as_view(), name="token_refresh"),
    path("users/me/", MeView.as_view(), name="user_me"),
    path("users/", UserListView.as_view(), name="user_list"),
]
