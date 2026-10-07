from rest_framework.permissions import BasePermission

from apps.identity.services import user_has_permission


class HasPermission(BasePermission):
    """
    Generic permission-codename check. Use via `permission_classes` combined
    with a `required_permission` attribute on the view, e.g.:

        class UserListView(APIView):
            permission_classes = [HasPermission]
            required_permission = "user.manage"

    This is deliberately the ONLY authorization primitive views use —
    no view should check `request.user.role == "admin"` or similar
    hardcoded checks; everything routes through the permission model.
    """

    message = "You do not have permission to perform this action."

    def has_permission(self, request, view):
        required = getattr(view, "required_permission", None)
        if required is None:
            # A view using this class must declare what it requires.
            return False
        return user_has_permission(request.user, required)


def require_permission(codename: str):
    """
    Class decorator / factory for function-based or generic views that want
    a one-off permission class without subclassing HasPermission each time.
    """

    class _Permission(HasPermission):
        required_permission = codename

    return _Permission
