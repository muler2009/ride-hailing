from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models

from apps.common.models import BaseModel


class RatingDirection(models.TextChoices):
    RIDER_TO_DRIVER = "RIDER_TO_DRIVER", "Rider rates Driver"
    DRIVER_TO_RIDER = "DRIVER_TO_RIDER", "Driver rates Rider"


class Rating(BaseModel):
    """
    One rating per direction per ride — a rider rating their driver and
    that driver rating the rider back are two separate rows, enforced by
    the unique constraint on (ride, direction). This is the polymorphic
    `direction` approach the architecture doc's ERD specifies, rather
    than two separate tables.

    The review text is optional: a star rating alone is the common case,
    and forcing a comment would depress submission rates for no gain.

    Note on guests: a guest rider (no account — see Phase 3) can rate
    their driver via the ride's tracking_token, so `rater` is nullable.
    A driver rating a guest rider back has nobody durable to attach that
    reputation to, so DRIVER_TO_RIDER is only accepted for rides with a
    registered rider — enforced in services.py, since it's a business
    rule about who can be rated, not a schema shape.
    """

    ride = models.ForeignKey("rides.Ride", on_delete=models.PROTECT, related_name="ratings")
    direction = models.CharField(max_length=20, choices=RatingDirection.choices)
    score = models.PositiveSmallIntegerField(validators=[MinValueValidator(1), MaxValueValidator(5)])
    review = models.CharField(max_length=1000, blank=True)
    rater = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="ratings_given",
        help_text="Null when a guest rider submitted the rating (no account to attribute it to).",
    )
    ratee = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="ratings_received",
        help_text="Null when rating a guest rider, who has no account.",
    )

    class Meta:
        db_table = "ratings_rating"
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["ride", "direction"], name="uniq_rating_per_ride_direction")
        ]
        indexes = [models.Index(fields=["ratee", "direction"])]

    def __str__(self):
        return f"Rating<{self.ride_id}:{self.direction}> {self.score}★"
