"""Server-owned entitlements; no public API can change a user's plan."""
from dataclasses import dataclass
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


def plan_limits(user):
    if user.plan not in PLAN_LIMITS:
        raise HTTPException(409, detail={"code": "PLAN_INVALID", "message": "План аккаунта требует проверки."})
    return PLAN_LIMITS[user.plan]
