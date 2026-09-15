"""Short transactions: reserve -> commit -> OpenAI -> finalize/release.

All quota mutations take the same user row lock used by chart creation/migration.
Dates are naive UTC to match the existing database convention.
"""
from datetime import datetime, timedelta, timezone
from uuid import uuid4
from fastapi import HTTPException
from sqlalchemy import exists
from database.queries import User, NatalChart, GPTMessage, GPTUsage
from modules.ai_subject import owned_subject, subject_fields, conversation_tables
from modules.plans import plan_limits, effective_plan

OPENAI_TIMEOUT_SECONDS = 60
RESERVATION_TTL = timedelta(minutes=5)


def utcnow():
    return datetime.utcnow()


def locked_user(db, user_id):
    user = db.query(User).filter(User.id == user_id).populate_existing().with_for_update().one()
    if user.plan != effective_plan(user, utcnow()):
        user.plan = "free"
        user.current_period_start = user.current_period_end = None
    return user


def backfill_user_history(db, user_id):
    """Caller holds user lock. Import retained legacy USER messages exactly once.

    Also covers old guest history after selected-chart migration. The SQL rollout
    does the initial bulk backfill; this repair path never edits GPTMessage.
    """
    messages = db.query(GPTMessage).join(NatalChart).filter(
        NatalChart.user_id == user_id, GPTMessage.role == "user",
        ~exists().where(GPTUsage.source_message_id == GPTMessage.id),
    ).all()
    for message in messages:
        db.add(GPTUsage(user_id=user_id, chart_id=message.chart_id,
                        source_message_id=message.id, status="succeeded", plan="free",
                        created_at=message.created_at or utcnow()))
    db.flush()


def valid_period(user, now):
    return bool(user.current_period_start and user.current_period_end and
                user.current_period_start <= now < user.current_period_end)


def quota_state(db, user, now):
    limits = plan_limits(user)
    rows = db.query(GPTUsage).filter(GPTUsage.user_id == user.id)
    if limits.gpt_limit_type == "billing_period":
        # The start is the period identity; extending the end must not reset usage.
        rows = rows.filter(GPTUsage.plan == "premium", GPTUsage.period_start == user.current_period_start)
    used = rows.filter(GPTUsage.status == "succeeded").count()
    reserved = rows.filter(GPTUsage.status == "reserved", GPTUsage.expires_at > now).count()
    period_ok = user.plan == "free" or valid_period(user, now)
    return {
        "plan": user.plan,
        "gpt_messages_used": used,
        "gpt_messages_reserved": reserved,
        "gpt_messages_limit": limits.gpt_message_limit,
        "gpt_limit_type": limits.gpt_limit_type,
        "current_period_start": user.current_period_start,
        "current_period_end": user.current_period_end,
        "gpt_period_valid": period_ok,
        "gpt_messages_available": max(0, limits.gpt_message_limit - used - reserved) if period_ok else 0,
    }


def account_usage(db, user_id):
    try:
        user = locked_user(db, user_id)
        backfill_user_history(db, user_id)
        result = quota_state(db, user, utcnow())
        result.update(saved_charts_used=db.query(NatalChart).filter_by(user_id=user_id).count(),
                      saved_charts_limit=plan_limits(user).max_saved_charts,
                      subscription_status=user.subscription_status,
                      scheduled_cancel_at=user.scheduled_cancel_at,
                      cancel_at_period_end=user.scheduled_cancel_at is not None,
                      can_manage_subscription=bool(user.payment_provider == "paddle" and user.provider_customer_id))
        db.commit()
        return result
    except BaseException:
        db.rollback()
        raise


def reserve(db, user_id, chart_id=None, *, relationship_id=None):
    try:
        user = locked_user(db, user_id)
        fields = subject_fields(chart_id, relationship_id)
        owned_subject(db, user_id, chart_id, relationship_id, lock=True)
        backfill_user_history(db, user_id)
        now = utcnow()
        db.query(GPTUsage).filter(GPTUsage.user_id == user_id, GPTUsage.status == "reserved",
                                 GPTUsage.expires_at <= now).update({"status": "released"}, synchronize_session=False)
        state = quota_state(db, user, now)
        if not state["gpt_period_valid"]:
            raise HTTPException(409, detail={"code": "GPT_PERIOD_INVALID", "plan": user.plan,
                                            "message": "Нет действующего периода Premium."})
        if not state["gpt_messages_available"]:
            raise HTTPException(409, detail={"code": "GPT_LIMIT_REACHED", "plan": user.plan,
                "used": state["gpt_messages_used"], "reserved": state["gpt_messages_reserved"],
                "limit": state["gpt_messages_limit"], "message": "Достигнут лимит GPT-сообщений аккаунта."})
        if db.query(GPTUsage).filter_by(user_id=user_id, **fields, status="reserved").filter(
                GPTUsage.expires_at > now).first():
            raise HTTPException(409, detail={"code": "GPT_REQUEST_IN_PROGRESS",
                                            "message": "Дождитесь ответа на предыдущий вопрос этой карты." if relationship_id is None else "Дождитесь ответа на предыдущий вопрос этого анализа."})
        reservation_id = uuid4()
        db.add(GPTUsage(id=reservation_id, user_id=user_id, **fields, status="reserved",
                        plan=user.plan, period_start=user.current_period_start if user.plan == "premium" else None,
                        period_end=user.current_period_end if user.plan == "premium" else None,
                        created_at=now, expires_at=now + RESERVATION_TTL))
        db.commit()
        return reservation_id
    except BaseException:
        db.rollback()
        raise


def finalize(db, user_id, reservation_id, question, answer):
    try:
        locked_user(db, user_id)
        reservation = db.query(GPTUsage).filter_by(id=reservation_id, user_id=user_id)
        item = reservation.populate_existing().one()
        owned_subject(db, user_id, item.chart_id, item.relationship_id, lock=True)
        item = reservation.populate_existing().with_for_update().one()
        if item.status != "reserved" or item.expires_at <= utcnow():
            raise HTTPException(409, detail={"code": "GPT_RESERVATION_EXPIRED",
                                            "message": "Запрос истёк. Квота не списана, повторите вопрос."})
        fields = subject_fields(item.chart_id, item.relationship_id)
        Message, _ = conversation_tables(item.relationship_id)
        user_message = Message(**fields, role="user", content=question, created_at=utcnow())
        db.add_all([user_message, Message(**fields, role="gpt", content=answer, created_at=utcnow())])
        db.flush()
        item.status = "succeeded"
        if item.relationship_id is None:
            item.source_message_id = user_message.id
        else:
            item.source_relationship_message_id = user_message.id
        from modules.ai_conversation import AIAnswer, save_memory
        if isinstance(answer, AIAnswer):
            for field, value in answer.metadata.items():
                setattr(item, field, value)
            item.summary_usage = answer.summary_usage or None
            save_memory(db, user_id, item.chart_id, answer.memory_update, relationship_id=item.relationship_id)
        db.commit()
    except BaseException:
        db.rollback()
        raise


def release(db, user_id, reservation_id):
    db.rollback()
    try:
        locked_user(db, user_id)
        # Never undo a successfully committed interaction, even after an ambiguous commit error.
        db.query(GPTUsage).filter_by(id=reservation_id, user_id=user_id, status="reserved").update(
            {"status": "released"}, synchronize_session=False)
        db.commit()
    except BaseException:
        db.rollback()
        raise  # If storage is unavailable the lease expires; it cannot finalize later.


def apply_plan(db, user, plan, period_start=None, period_end=None):
    """Caller owns the user row lock and transaction (including webhook ledger)."""
    def utc(value):
        return value.astimezone(timezone.utc).replace(tzinfo=None) if value and value.tzinfo else value
    start, end = utc(period_start), utc(period_end)
    if plan not in ("free", "premium") or (plan == "premium" and (not start or not end or start >= end)):
        raise ValueError("Invalid plan or billing period")
    if plan == "premium":
        # No resetting quota by shifting the start inside an existing period.
        previous = db.query(GPTUsage).filter_by(user_id=user.id, plan="premium").all()
        periods = [(row.period_start, row.period_end) for row in previous]
        if user.current_period_start:
            periods.append((user.current_period_start, user.current_period_end))
        if any(a and b and a != start and start < b and end > a for a, b in periods):
            raise ValueError("Billing periods must not overlap")
    user.plan = plan
    user.current_period_start = start if plan == "premium" else None
    user.current_period_end = end if plan == "premium" else None


def set_plan(db, user_id, plan, period_start=None, period_end=None):
    """Trusted backend helper only; not exposed via HTTP. Owns its transaction."""
    try:
        user = locked_user(db, user_id)
        apply_plan(db, user, plan, period_start, period_end)
        db.commit()
    except BaseException:
        db.rollback()
        raise
