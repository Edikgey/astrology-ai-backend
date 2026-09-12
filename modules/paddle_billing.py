"""Sandbox-only billing adapter. Never log request bodies or provider exceptions."""
import os
import re
import time
from datetime import datetime, timezone
from types import SimpleNamespace

from fastapi import HTTPException
from paddle_billing import Client, Environment, Options
from paddle_billing.Notifications import Secret, Verifier
from paddle_billing.Notifications.PaddleSignature import PaddleSignature
from paddle_billing.Entities.Shared import CustomData
from paddle_billing.Resources.Transactions.Operations import CreateTransaction
from paddle_billing.Resources.Transactions.Operations.Create import TransactionCreateItem
from paddle_billing.Resources.CustomerPortalSessions.Operations import CreateCustomerPortalSession
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from database.queries import User, PaddleCheckout, PaddleEvent
from modules.usage import locked_user, apply_plan

EVENTS = {"subscription." + suffix for suffix in
          ("created", "updated", "activated", "resumed", "past_due", "paused", "canceled")}


def premium_price_id():
    value = os.getenv("PADDLE_PREMIUM_PRICE_ID", "")
    if not re.fullmatch(r"pri_[a-z0-9]{26}", value):
        raise HTTPException(503, "Paddle price is not configured")
    return value


def paddle_client():
    key = os.getenv("PADDLE_SANDBOX_API_KEY", "")
    if not key.startswith("pdl_sdbx_apikey_"):
        raise HTTPException(503, "Paddle Sandbox is not configured")
    return Client(key, options=Options(environment=Environment.SANDBOX), retry_count=0, timeout=10)


def verify_signature(body, headers):
    secret = os.getenv("PADDLE_WEBHOOK_SECRET", "")
    if not secret:
        raise HTTPException(503, "Paddle webhook is not configured")
    try:
        # Also reject future timestamps and enforce tolerance even if SDK TEST_MODE is set.
        timestamp, _ = PaddleSignature.parse(headers.get("paddle-signature", ""))
        if abs(time.time() - timestamp) > 5:
            raise ValueError("Timestamp outside tolerance")
        valid = Verifier().verify(SimpleNamespace(body=body, headers=headers), Secret(secret))
    except (ValueError, TypeError, UnicodeError, ConnectionRefusedError):
        valid = False
    if not valid:
        raise HTTPException(401, "Invalid Paddle signature")


def utc_date(value):
    if not isinstance(value, str):
        raise ValueError("Missing timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Timestamp must include timezone")
    return parsed.astimezone(timezone.utc).replace(tzinfo=None)


def create_checkout(db, user_id):
    price_id, client = premium_price_id(), paddle_client()
    try:
        user = locked_user(db, user_id)
        if user.plan == "premium" or user.subscription_status in ("active", "past_due", "paused", "trialing"):
            raise HTTPException(409, "Use Manage subscription for your existing subscription")
        checkout = db.query(PaddleCheckout).filter_by(user_id=user_id).order_by(PaddleCheckout.created_at.desc()).first()
        if checkout and checkout.state in ("creating", "ready"):
            if not checkout.transaction_id:
                raise HTTPException(409, "Checkout creation is pending verification; contact support before retrying")
            db.commit()
            return {"transaction_id": checkout.transaction_id, "price_id": price_id}
        checkout = PaddleCheckout(user_id=user_id)
        db.add(checkout)
        db.flush()
        binding = checkout.id
        customer_id = user.provider_customer_id
        # Persist the intent before the network call. Ambiguous failures must never
        # automatically create a second chargeable transaction.
        db.commit()
        operation = CreateTransaction(
            items=[TransactionCreateItem(price_id=price_id, quantity=1)],
            custom_data=CustomData({"user_id": str(user_id), "checkout_binding": binding}),
        )
        if customer_id:
            operation.customer_id = customer_id
        transaction = client.transactions.create(operation)
        locked_user(db, user_id)
        checkout = db.get(PaddleCheckout, binding)
        checkout.transaction_id = transaction.id
        if checkout.state == "creating":
            checkout.state = "ready"
        db.commit()
        return {"transaction_id": transaction.id, "price_id": price_id}
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise HTTPException(502, "Unable to create checkout; contact support before retrying") from None


def create_portal(db, user_id):
    user = db.get(User, user_id)
    if user.payment_provider != "paddle" or not user.provider_customer_id or not user.provider_subscription_id:
        raise HTTPException(409, "No Paddle subscription for this account")
    customer_id, subscription_id = user.provider_customer_id, user.provider_subscription_id
    db.rollback()  # No database transaction while waiting for Paddle.
    client = paddle_client()
    try:
        session = client.customer_portal_sessions.create(customer_id,
            CreateCustomerPortalSession(subscription_ids=[subscription_id]))
        return {"url": session.urls.general.overview}
    except Exception:
        raise HTTPException(502, "Unable to open subscription management") from None


def sync_subscription(db, event):
    data = event["data"]
    subscription_id, customer_id = data["id"], data["customer_id"]
    if not re.fullmatch(r"sub_[a-z0-9]{26}", subscription_id) or not re.fullmatch(r"ctm_[a-z0-9]{26}", customer_id):
        raise ValueError("Invalid provider IDs")
    custom = data.get("custom_data") or {}
    known = db.query(User).filter_by(provider_subscription_id=subscription_id).first()
    if known:
        user = locked_user(db, known.id)
        if user.provider_customer_id != customer_id:
            raise ValueError("Customer ownership mismatch")
        if custom.get("user_id") is not None and str(custom["user_id"]) != str(user.id):
            raise ValueError("User ownership mismatch")
    else:
        binding = custom.get("checkout_binding")
        checkout = db.get(PaddleCheckout, binding) if isinstance(binding, str) else None
        if not checkout or str(custom.get("user_id")) != str(checkout.user_id):
            raise ValueError("Unknown checkout owner")
        user = locked_user(db, checkout.user_id)
        # Refresh after the user lock: another event may have bound the checkout.
        db.refresh(checkout)
        if checkout.subscription_id and checkout.subscription_id != subscription_id:
            raise ValueError("Checkout already bound")
        if user.provider_subscription_id and user.provider_subscription_id != subscription_id:
            if user.subscription_status != "canceled" or checkout.state == "completed":
                return "ignored_old_or_second_subscription"
        if user.provider_customer_id and user.provider_customer_id != customer_id:
            raise ValueError("Customer ownership mismatch")
        # Do not associate this Paddle customer with a different local account.
        owner = db.query(User).filter_by(provider_customer_id=customer_id).first()
        if owner and owner.id != user.id:
            raise ValueError("Customer already owned")
        checkout.subscription_id = subscription_id
        checkout.state = "completed"
    occurred = utc_date(event["occurred_at"])
    if user.paddle_updated_at and occurred <= user.paddle_updated_at:
        return "ignored_stale"
    status = data["status"]
    if status not in ("active", "past_due", "paused", "canceled", "trialing"):
        raise ValueError("Unsupported subscription status")
    items = data.get("items") or []
    eligible = len(items) == 1 and items[0].get("quantity") == 1 and (
        items[0].get("price", {}).get("id") == premium_price_id())
    if status == "active" and eligible:
        period = data.get("current_billing_period") or {}
        start, end = utc_date(period.get("starts_at")), utc_date(period.get("ends_at"))
        cycle = data.get("billing_cycle") or {}
        if cycle != {"interval": "month", "frequency": 1}:
            raise ValueError("Unexpected billing cycle")
        apply_plan(db, user, "premium", start, end)
    elif status in ("canceled", "paused") or not eligible or status == "trialing":
        apply_plan(db, user, "free")
    # past_due preserves the last confirmed period, never grants a new unpaid
    # period. Existing GPT_PERIOD_INVALID fail-safe applies after that period.
    change = data.get("scheduled_change") or {}
    user.scheduled_cancel_at = utc_date(change["effective_at"]) if change.get("action") == "cancel" else None
    user.payment_provider = "paddle"
    user.provider_customer_id = customer_id
    user.provider_subscription_id = subscription_id
    user.subscription_status = status
    user.paddle_updated_at = occurred
    return "processed"


def process_event(db, event):
    try:
        event_id, event_type = event["event_id"], event["event_type"]
        if not re.fullmatch(r"evt_[a-z0-9]{26}", event_id):
            raise ValueError("Invalid event ID")
        occurred = utc_date(event["occurred_at"])
        insert = pg_insert if db.bind.dialect.name == "postgresql" else sqlite_insert
        result = db.execute(insert(PaddleEvent).values(event_id=event_id, event_type=event_type,
            occurred_at=occurred, processed_at=datetime.utcnow(), outcome="processed"
        ).on_conflict_do_nothing(index_elements=["event_id"]))
        if result.rowcount == 0:
            db.commit()
            return "duplicate"
        outcome = sync_subscription(db, event) if event_type in EVENTS else "ignored_event_type"
        db.get(PaddleEvent, event_id).outcome = outcome
        db.commit()  # Event ledger and entitlement changes commit together.
        return outcome
    except (ValueError, KeyError, TypeError, AttributeError):
        db.rollback()
        raise HTTPException(400, "Invalid or unrecognized Paddle subscription event") from None
    except Exception:
        db.rollback()
        raise
