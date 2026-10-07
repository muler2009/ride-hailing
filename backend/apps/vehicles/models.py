from django.conf import settings
from django.db import models

from apps.common.models import BaseModel, SoftDeleteModel


class VehicleType(BaseModel):
    """
    A category of vehicle (Sedan, SUV, Van, Moto, ...). Referenced by
    dispatch (candidate filtering) and pricing (rate tables) in later
    phases — kept as its own table rather than a hardcoded choice field so
    operations can add/retire types and capacities without a deploy.
    """

    name = models.CharField(max_length=50, unique=True)
    description = models.CharField(max_length=255, blank=True)
    passenger_capacity = models.PositiveSmallIntegerField(default=4)
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "vehicles_vehicle_type"
        ordering = ["name"]

    def __str__(self):
        return self.name


class Vehicle(BaseModel, SoftDeleteModel):
    """
    A vehicle registered by a driver. A driver may register more than one,
    but per the architecture doc only one may be active at a time — enforced
    in apps/vehicles/services.py (set_active_vehicle), not by a DB
    constraint alone, since "deactivate the others" is a multi-row rule.
    """

    driver = models.ForeignKey(
        "drivers.Driver", on_delete=models.CASCADE, related_name="vehicles"
    )
    vehicle_type = models.ForeignKey(
        VehicleType, on_delete=models.PROTECT, related_name="vehicles"
    )
    make = models.CharField(max_length=100)
    model = models.CharField(max_length=100)
    year = models.PositiveSmallIntegerField()
    color = models.CharField(max_length=50, blank=True)
    license_plate = models.CharField(max_length=20, unique=True, db_index=True)
    is_active = models.BooleanField(
        default=False,
        help_text="The vehicle a driver is currently operating under. Only one per driver.",
    )

    class Meta:
        db_table = "vehicles_vehicle"
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["driver", "is_active"])]

    def __str__(self):
        return f"{self.make} {self.model} ({self.license_plate})"


class VehicleDocumentType(models.TextChoices):
    REGISTRATION = "REGISTRATION", "Vehicle Registration"
    INSURANCE = "INSURANCE", "Insurance"
    INSPECTION = "INSPECTION", "Inspection Certificate"


class VehicleDocumentStatus(models.TextChoices):
    PENDING_REVIEW = "PENDING_REVIEW", "Pending Review"
    VERIFIED = "VERIFIED", "Verified"
    REJECTED = "REJECTED", "Rejected"


class VehicleDocument(BaseModel):
    vehicle = models.ForeignKey(Vehicle, on_delete=models.CASCADE, related_name="documents")
    document_type = models.CharField(max_length=20, choices=VehicleDocumentType.choices)
    file = models.FileField(upload_to="vehicle_documents/%Y/%m/")
    status = models.CharField(
        max_length=20,
        choices=VehicleDocumentStatus.choices,
        default=VehicleDocumentStatus.PENDING_REVIEW,
    )
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="vehicle_documents_reviewed",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    rejection_reason = models.CharField(max_length=255, blank=True)
    expires_at = models.DateField(null=True, blank=True)

    class Meta:
        db_table = "vehicles_vehicle_document"
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["vehicle", "document_type"], name="uniq_vehicle_document_type"
            )
        ]

    def __str__(self):
        return f"{self.vehicle_id} - {self.document_type}"
