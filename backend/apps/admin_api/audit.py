"""
record_audit() is the only way an AuditLog entry is ever created. It's
deliberately forgiving about failure (logs and moves on rather than
raising), matching the best-effort cross-context pattern used for
notifications, pricing, and dispatch throughout this codebase: a failure
to *record* an administrative action must never roll back the action
itself, which has already legitimately happened.

That tolerance is a real trade-off worth naming: it means an audit trail
can in principle have a gap if the log write fails. The alternative —
failing the whole admin action because logging failed — would be worse
operationally (an admin unable to suspend a fraudulent driver because a
log table is unavailable), and the failure is loud in application logs
either way. For a compliance context demanding guaranteed-complete
audit, this call should move inside the caller's transaction instead.
"""
from apps.admin_api.models import AuditLog


def record_audit(*, actor, action: str, target_type: str, target_id, reason: str = "", changes: dict = None) -> None:
    try:
        AuditLog.objects.create(
            actor=actor,
            actor_email=getattr(actor, "email", "") or "",
            action=action,
            target_type=target_type,
            target_id=str(target_id),
            reason=reason,
            changes=changes or {},
        )
    except Exception:
        import logging

        logging.getLogger(__name__).exception(
            "Failed to record audit entry: %s %s:%s", action, target_type, target_id
        )
