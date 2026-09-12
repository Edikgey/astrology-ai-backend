"""Server-owned entitlements; no public API can change a user's plan."""
from dataclasses import dataclass
from datetime import datetime
from fastapi import HTTPException


@dataclass(frozen=True)
class PlanLimits:
    max_saved_charts: int
    gpt_message_limit: int
    gpt_limit_type: str


PLAN_LIMITS = {
    "free": PlanLimits(3, 10, "lifetime"),
    "premium": PlanLimits(10, 300, "billing_period"),
}


def effective_plan(user, now=None):
    # Enforce a confirmed scheduled end even when the final webhook is delayed.
    # Unscheduled expiry keeps the existing Premium-period fail-safe unchanged.
    if (getattr(user, "payment_provider", None) == "paddle" and
            getattr(user, "scheduled_cancel_at", None) and
            user.scheduled_cancel_at <= (now or datetime.utcnow())):
        return "free"
    return user.plan


def plan_limits(user):
    if user.plan not in PLAN_LIMITS:
        raise HTTPException(409, detail={"code": "PLAN_INVALID", "message": "План аккаунта требует проверки."})
    return PLAN_LIMITS[effective_plan(user)]
