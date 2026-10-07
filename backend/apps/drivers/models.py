from django.conf import settings
from django.db import models

from apps.common.models import BaseModel


class DriverApprovalStatus(models.TextChoices):
    PENDING_REVIEW = "PENDING_REVIEW", "Pending Review"
    APPROVED = "APPROVED", "Approved"
    REJECTED = "REJECTED", "Rejected"
    SUSPENDED = "SUSPENDED", "Suspended"


class Driver(BaseModel):
    """
    Driver-specific profile data, 1:1 with User. A driver cannot accept ride
    requests until `approval_status` is APPROVED — enforced by
    apps/drivers/services.py, not by any view directly setting the field.
    """

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="driver_profile"
    )
    license_number = models.CharField(max_length=50)
    license_expiry = models.DateField()
    date_of_birth = models.DateField(null=True, blank=True)

    approval_status = models.CharField(
        max_length=20,
        choices=DriverApprovalStatus.choices,
        default=DriverApprovalStatus.PENDING_REVIEW,
        db_index=True,
    )
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="drivers_reviewed",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    rejection_reason = models.CharField(max_length=255, blank=True)
    suspension_reason = models.CharField(max_length=255, blank=True)

    # Dispatch-relevant fields (Phase 4). Current location is deliberately
    # NOT stored here — it lives in Redis only (apps/dispatch/geo.py), per
    # the architecture doc's rule against writing every GPS-adjacent update
    # to Postgres. `is_available` is a low-frequency explicit toggle, not a
    # continuous stream, so it's fine as a normal column.
    is_available = models.BooleanField(
        default=False, db_index=True, help_text="Online/offline toggle, set by the driver."
    )
    average_rating = models.DecimalField(
        max_digits=3, decimal_places=2, null=True, blank=True,
        help_text="Populated once Ratings (Phase 11) exists. Null is treated as neutral in ranking.",
    )
    total_offers_sent = models.PositiveIntegerField(default=0)
    total_offers_accepted = models.PositiveIntegerField(default=0)

    class Meta:
        db_table = "drivers_driver"
        ordering = ["-created_at"]

    def __str__(self):
        return f"Driver<{self.user.email}> [{self.approval_status}]"

    @property
    def is_approved(self):
        return self.approval_status == DriverApprovalStatus.APPROVED

    @property
    def acceptance_rate(self):
        """Fraction of offers this driver has accepted. None if they've never been offered one yet."""
        if self.total_offers_sent == 0:
            return None
        return self.total_offers_accepted / self.total_offers_sent


class DriverDocumentType(models.TextChoices):
    DRIVERS_LICENSE = "DRIVERS_LICENSE", "Driver's License"
    BACKGROUND_CHECK = "BACKGROUND_CHECK", "Background Check"
    PROFILE_PHOTO = "PROFILE_PHOTO", "Profile Photo"


class DriverDocumentStatus(models.TextChoices):
    PENDING_REVIEW = "PENDING_REVIEW", "Pending Review"
    VERIFIED = "VERIFIED", "Verified"
    REJECTED = "REJECTED", "Rejected"


class DriverDocument(BaseModel):
    driver = models.ForeignKey(Driver, on_delete=models.CASCADE, related_name="documents")
    document_type = models.CharField(max_length=20, choices=DriverDocumentType.choices)
    file = models.FileField(upload_to="driver_documents/%Y/%m/")
    status = models.CharField(
        max_length=20,
        choices=DriverDocumentStatus.choices,
        default=DriverDocumentStatus.PENDING_REVIEW,
    )
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="driver_documents_reviewed",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    rejection_reason = models.CharField(max_length=255, blank=True)
    expires_at = models.DateField(null=True, blank=True)

    class Meta:
        db_table = "drivers_driver_document"
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["driver", "document_type"], name="uniq_driver_document_type"
            )
        ]

    def __str__(self):
        return f"{self.driver_id} - {self.document_type}"
