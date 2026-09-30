"""Internal access decisions from authenticated, owner-checked billing adapters.

Adapters own event deduplication/order, provider metadata and the transaction.
This module neither accepts client payment confirmations nor commits independently.
"""
from datetime import timezone

from modules.usage import apply_plan


def apply_subscription_access(db, user, decision, *, period_start=None,
                              period_end=None, scheduled_cancel_at=None):
    """Apply grant/revoke/retain under the caller's User row lock.

    Grant requires a confirmed paid period; retain never extends it. Every call
    supplies the current confirmed cancellation schedule (None clears it).
    The existing plan writer remains authoritative for quota-period validation.
    """
    if decision not in ("grant", "revoke", "retain"):
        raise ValueError("Unknown subscription access decision")
    if decision != "grant" and (period_start is not None or period_end is not None):
        raise ValueError("Only a grant may set a billing period")
    cancel_at = scheduled_cancel_at
    if cancel_at is not None and cancel_at.tzinfo is not None:
        cancel_at = cancel_at.astimezone(timezone.utc).replace(tzinfo=None)
    if decision == "grant":
        apply_plan(db, user, "premium", period_start, period_end)
    elif decision == "revoke":
        apply_plan(db, user, "free")
    user.scheduled_cancel_at = cancel_at
