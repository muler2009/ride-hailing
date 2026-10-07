from django.db import transaction
from django.utils import timezone

from apps.drivers.models import (
    Driver,
    DriverApprovalStatus,
    DriverDocument,
)


class InvalidDriverTransition(Exception):
    pass


def _record_audit(actor, action: str, driver: Driver, reason: str = "") -> None:
    from apps.admin_api.audit import record_audit

    record_audit(actor=actor, action=action, target_type="Driver", target_id=driver.id, reason=reason)


def _notify_driver_event(driver: Driver, event_type: str, title: str, body: str) -> None:
    """Best-effort, same tolerance pattern used throughout this codebase for cross-context calls."""
    try:
        from apps.notifications.services import notify

        notify(event_type=event_type, title=title, body=body, recipient_user=driver.user)
    except Exception:
        import logging

        logging.getLogger(__name__).exception(
            "Notification dispatch failed for driver %s event %s", driver.id, event_type
        )


# Explicit transition table: {current_status: {allowed_next_status, ...}}.
# Mirrors the ride state machine's philosophy from the architecture doc —
# one table, one enforcement point, no view sets approval_status directly.
_ALLOWED_TRANSITIONS = {
    DriverApprovalStatus.PENDING_REVIEW: {
        DriverApprovalStatus.APPROVED,
        DriverApprovalStatus.REJECTED,
    },
    DriverApprovalStatus.REJECTED: {
        DriverApprovalStatus.PENDING_REVIEW,  # driver resubmits
    },
    DriverApprovalStatus.APPROVED: {
        DriverApprovalStatus.SUSPENDED,
    },
    DriverApprovalStatus.SUSPENDED: {
        DriverApprovalStatus.APPROVED,  # reactivation
    },
}


@transaction.atomic
def apply_as_driver(user, *, license_number: str, license_expiry, date_of_birth=None) -> Driver:
    """
    Creates or updates the calling user's Driver profile. Safe to call again
    after a rejection: it moves the driver back to PENDING_REVIEW so the
    resubmission is reviewed rather than silently staying REJECTED.
    """
    driver, created = Driver.objects.get_or_create(
        user=user,
        defaults={
            "license_number": license_number,
            "license_expiry": license_expiry,
            "date_of_birth": date_of_birth,
        },
    )
    if not created:
        driver.license_number = license_number
        driver.license_expiry = license_expiry
        if date_of_birth is not None:
            driver.date_of_birth = date_of_birth
        if driver.approval_status == DriverApprovalStatus.REJECTED:
            _transition(driver, DriverApprovalStatus.PENDING_REVIEW)
        driver.save()
    return driver


def submit_document(driver: Driver, *, document_type: str, file, expires_at=None) -> DriverDocument:
    """Upserts a document by type — a resubmission replaces the prior file/status."""
    document, _created = DriverDocument.objects.update_or_create(
        driver=driver,
        document_type=document_type,
        defaults={
            "file": file,
            "expires_at": expires_at,
            "status": "PENDING_REVIEW",
            "reviewed_by": None,
            "reviewed_at": None,
            "rejection_reason": "",
        },
    )
    return document


def _transition(driver: Driver, new_status: str) -> None:
    allowed = _ALLOWED_TRANSITIONS.get(driver.approval_status, set())
    if new_status not in allowed:
        raise InvalidDriverTransition(
            f"Cannot move driver from {driver.approval_status} to {new_status}."
        )
    driver.approval_status = new_status


@transaction.atomic
def approve_driver(driver: Driver, reviewed_by) -> Driver:
    _transition(driver, DriverApprovalStatus.APPROVED)
    driver.reviewed_by = reviewed_by
    driver.reviewed_at = timezone.now()
    driver.rejection_reason = ""
    driver.save(update_fields=["approval_status", "reviewed_by", "reviewed_at", "rejection_reason"])
    _record_audit(reviewed_by, "DRIVER_APPROVED", driver)
    _notify_driver_event(driver, "DRIVER_APPROVED", "You're approved!", "Your driver application has been approved — you can now go online.")
    return driver


@transaction.atomic
def reject_driver(driver: Driver, reviewed_by, reason: str) -> Driver:
    _transition(driver, DriverApprovalStatus.REJECTED)
    driver.reviewed_by = reviewed_by
    driver.reviewed_at = timezone.now()
    driver.rejection_reason = reason
    driver.save(update_fields=["approval_status", "reviewed_by", "reviewed_at", "rejection_reason"])
    _record_audit(reviewed_by, "DRIVER_REJECTED", driver, reason=reason)
    return driver


@transaction.atomic
def suspend_driver(driver: Driver, reviewed_by, reason: str) -> Driver:
    _transition(driver, DriverApprovalStatus.SUSPENDED)
    driver.reviewed_by = reviewed_by
    driver.reviewed_at = timezone.now()
    driver.suspension_reason = reason
    driver.save(update_fields=["approval_status", "reviewed_by", "reviewed_at", "suspension_reason"])
    _record_audit(reviewed_by, "DRIVER_SUSPENDED", driver, reason=reason)
    _notify_driver_event(driver, "DRIVER_SUSPENDED", "Account suspended", f"Your driver account has been suspended: {reason}")
    return driver


@transaction.atomic
def reactivate_driver(driver: Driver, reviewed_by) -> Driver:
    _transition(driver, DriverApprovalStatus.APPROVED)
    driver.reviewed_by = reviewed_by
    driver.reviewed_at = timezone.now()
    driver.suspension_reason = ""
    driver.save(update_fields=["approval_status", "reviewed_by", "reviewed_at", "suspension_reason"])
    _record_audit(reviewed_by, "DRIVER_REACTIVATED", driver)
    return driver


def set_availability(driver: Driver, available: bool) -> Driver:
    """
    Online/offline toggle. A driver must be APPROVED to go online. Going
    offline immediately removes them from dispatch candidacy by clearing
    their Redis location entry — not just leaving it to expire on its own
    TTL, since an explicit "I'm going offline" signal should take effect
    right away.
    """
    if available and driver.approval_status != DriverApprovalStatus.APPROVED:
        raise ValueError("Only an approved driver may go online.")

    driver.is_available = available
    driver.save(update_fields=["is_available"])

    if not available:
        from apps.locations.geo import remove_driver_location

        remove_driver_location(driver.id)

    return driver
