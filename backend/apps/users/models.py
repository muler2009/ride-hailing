from django.contrib.auth.base_user import AbstractBaseUser, BaseUserManager
from django.contrib.auth.models import PermissionsMixin
from django.db import models

from apps.common.models import BaseModel, SoftDeleteModel


class UserManager(BaseUserManager):
    use_in_migrations = True

    def _create_user(self, email, password, **extra_fields):
        if not email:
            raise ValueError("An email address is required.")
        email = self.normalize_email(email)
        user = self.model(email=email, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_user(self, email, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", False)
        extra_fields.setdefault("is_superuser", False)
        return self._create_user(email, password, **extra_fields)

    def create_superuser(self, email, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        extra_fields.setdefault("is_active", True)
        if extra_fields.get("is_staff") is not True:
            raise ValueError("Superuser must have is_staff=True.")
        if extra_fields.get("is_superuser") is not True:
            raise ValueError("Superuser must have is_superuser=True.")
        return self._create_user(email, password, **extra_fields)


class User(AbstractBaseUser, PermissionsMixin, BaseModel, SoftDeleteModel):
    """
    Platform-wide identity record. Role-specific profile data (Rider,
    Driver, ...) lives in their own apps' models, each in a 1:1 with this
    table, per the bounded-context split in the architecture doc — this
    model owns authentication only, not domain profile data.

    Credentials for regular platform users (riders, drivers, ...) live in
    Keycloak, not here — `keycloak_id` links this row to that identity, and
    such users are given an unusable local password
    (see apps/identity/services.py's sync_user_from_keycloak_claims and
    apps/identity/authentication.py's KeycloakAuthentication). The local
    password field remains usable ONLY for Django-admin staff accounts
    created via `createsuperuser`, which authenticate against the admin
    site directly rather than through the API.

    Phone number is optional-but-unique so it can become a second login
    identifier (OTP-based) in Keycloak without a schema change here.
    """

    keycloak_id = models.CharField(
        max_length=255, unique=True, null=True, blank=True, db_index=True
    )
    email = models.EmailField(unique=True, db_index=True)
    phone_number = models.CharField(
        max_length=32, unique=True, null=True, blank=True, db_index=True
    )
    first_name = models.CharField(max_length=150, blank=True)
    last_name = models.CharField(max_length=150, blank=True)

    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)
    is_suspended = models.BooleanField(default=False)
    suspended_at = models.DateTimeField(null=True, blank=True)
    suspended_reason = models.CharField(max_length=255, blank=True)

    # Notification preferences (Phase 10). Defaulting all to True matches
    # FR-NOT-01's "based on user preference" — the preference exists and is
    # honored, but starts opted-in; IN_APP notifications aren't gated by
    # any of these three, since there's no external delivery cost to them.
    notify_email = models.BooleanField(default=True)
    notify_sms = models.BooleanField(default=True)
    notify_push = models.BooleanField(default=True)
    push_token = models.CharField(max_length=255, blank=True, help_text="FCM device token, if registered.")

    objects = UserManager()

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = []

    class Meta:
        db_table = "users_user"
        ordering = ["-created_at"]

    def __str__(self):
        return self.email

    @property
    def full_name(self):
        return f"{self.first_name} {self.last_name}".strip()

    def suspend(self, reason: str = ""):
        from django.utils import timezone

        self.is_suspended = True
        self.suspended_at = timezone.now()
        self.suspended_reason = reason
        self.is_active = False
        self.save(update_fields=["is_suspended", "suspended_at", "suspended_reason", "is_active"])

    def reactivate(self):
        self.is_suspended = False
        self.suspended_at = None
        self.suspended_reason = ""
        self.is_active = True
        self.save(update_fields=["is_suspended", "suspended_at", "suspended_reason", "is_active"])
