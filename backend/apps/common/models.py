import uuid

from django.db import models
from django.utils import timezone


class TimeStampedModel(models.Model):
    """Base model adding created/updated timestamps to every table."""

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class SoftDeleteQuerySet(models.QuerySet):
    def alive(self):
        return self.filter(is_deleted=False)

    def dead(self):
        return self.filter(is_deleted=True)


class SoftDeleteManager(models.Manager):
    """Default manager excludes soft-deleted rows. Use `all_objects` to include them."""

    def get_queryset(self):
        return SoftDeleteQuerySet(self.model, using=self._db).alive()


class SoftDeleteModel(models.Model):
    """
    Soft-delete mixin. Records are never physically removed via the ORM's
    default manager path — `delete()` flips a flag instead.

    NOTE: financial and ride-history tables must NOT use this mixin; those
    are append-only and immutable per the architecture doc.
    """

    is_deleted = models.BooleanField(default=False, db_index=True)
    deleted_at = models.DateTimeField(null=True, blank=True)

    objects = SoftDeleteManager()
    all_objects = models.Manager()

    class Meta:
        abstract = True

    def delete(self, using=None, keep_parents=False, hard=False):
        if hard:
            return super().delete(using=using, keep_parents=keep_parents)
        self.is_deleted = True
        self.deleted_at = timezone.now()
        self.save(update_fields=["is_deleted", "deleted_at"])
        return (1, {self.__class__.__name__: 1})

    def restore(self):
        self.is_deleted = False
        self.deleted_at = None
        self.save(update_fields=["is_deleted", "deleted_at"])


class UUIDPrimaryKeyModel(models.Model):
    """
    UUID primary keys across the platform: they don't leak sequential counts
    (e.g. total ride volume) through API responses, and they're safe to
    generate client-side later if ever needed (e.g. offline-created records).
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    class Meta:
        abstract = True


class BaseModel(UUIDPrimaryKeyModel, TimeStampedModel):
    class Meta:
        abstract = True
