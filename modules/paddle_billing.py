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

from database.queries import User, PaddleCheckout, PaddleEvent, LavaCheckout
from modules.usage import locked_user
from modules.subscription_access import apply_subscription_access
from modules.billing_conflicts import conflict_for, record_conflict, release_legacy_unpaid_owner, locked_billing_user
from modules.plans import effective_plan

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
        release_legacy_unpaid_owner(db, user)
        # The current schema has one billing owner per account. Switching it
        # needs an explicit migration policy, not an incidental checkout.
        if user.payment_provider not in (None, "paddle"):
            raise HTTPException(409, "Account billing is managed by another provider")
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


def checkout_for_event(db, data):
    custom = data.get("custom_data") or {}
    binding = custom.get("checkout_binding")
    checkout = db.get(PaddleCheckout, binding) if isinstance(binding, str) else None
    if checkout is None and not binding:
        checkout = db.query(PaddleCheckout).filter_by(subscription_id=data["id"]).first()
    if checkout is None:
        raise ValueError("Unknown checkout owner")
    if custom.get("user_id") is not None and str(custom["user_id"]) != str(checkout.user_id):
        raise ValueError("User ownership mismatch")
    if binding and str(custom.get("user_id")) != str(checkout.user_id):
        raise ValueError("User ownership mismatch")
    if checkout.subscription_id and checkout.subscription_id != data["id"]:
        raise ValueError("Checkout already bound")
    return checkout


def confirm_initial_payment(transaction_id, subscription_id, customer_id, binding, user_id):
    """Read-only provider verification, outside the webhook DB transaction."""
    if not transaction_id:
        raise HTTPException(503, "Checkout binding is pending")
    try:
        transaction = paddle_client().transactions.get(transaction_id)
    except Exception:
        raise HTTPException(503, "Payment confirmation is temporarily unavailable") from None
    if str(transaction.status.value) != "completed" or transaction.subscription_id != subscription_id:
        raise HTTPException(503, "Payment confirmation is pending")
    custom = transaction.custom_data.data if transaction.custom_data else {}
    if (transaction.id != transaction_id or transaction.customer_id != customer_id
            or custom.get("checkout_binding") != binding or str(custom.get("user_id")) != str(user_id)
            or len(transaction.items) != 1 or transaction.items[0].quantity != 1
            or transaction.items[0].price.id != premium_price_id()):
        raise ValueError("Paid transaction binding mismatch")


def sync_subscription(db, event):
    data = event["data"]
    subscription_id, customer_id = data["id"], data["customer_id"]
    if not re.fullmatch(r"sub_[a-z0-9]{26}", subscription_id) or not re.fullmatch(r"ctm_[a-z0-9]{26}", customer_id):
        raise ValueError("Invalid provider IDs")
    checkout = checkout_for_event(db, data)
    user = locked_billing_user(db, checkout.user_id)
    release_legacy_unpaid_owner(db, user)
    db.refresh(checkout)
    if checkout.subscription_id and checkout.subscription_id != subscription_id:
        raise ValueError("Checkout already bound")
    same = user.payment_provider == "paddle" and user.provider_subscription_id == subscription_id
    if user.payment_provider == "paddle" and user.provider_customer_id and user.provider_customer_id != customer_id:
        raise ValueError("Customer ownership mismatch")
    owner = db.query(User).filter_by(payment_provider="paddle", provider_customer_id=customer_id).first()
    if owner and owner.id != user.id:
        raise ValueError("Customer already owned")
    occurred = utc_date(event["occurred_at"])
    status = data["status"]
    if status not in ("active", "past_due", "paused", "canceled", "trialing"):
        raise ValueError("Unsupported subscription status")
    items = data.get("items") or []
    eligible = len(items) == 1 and items[0].get("quantity") == 1 and items[0].get("price", {}).get("id") == premium_price_id()
    decision, start, end = "retain", None, None
    if status == "active" and eligible:
        period = data.get("current_billing_period") or {}
        start, end = utc_date(period.get("starts_at")), utc_date(period.get("ends_at"))
        if start >= end or data.get("billing_cycle") != {"interval": "month", "frequency": 1}:
            raise ValueError("Unexpected billing period")
        decision = "grant"
    elif status in ("canceled", "paused", "trialing") or not eligible:
        decision = "revoke"
    existing = conflict_for(db, "paddle", subscription_id)
    # Retain existing same-provider resubscribe after cancellation. An already
    # recorded conflict must never acquire the primary slot, even after expiry.
    resubscribe = (user.payment_provider == "paddle" and user.subscription_status == "canceled"
                   and effective_plan(user) == "free" and checkout.state == "ready"
                   and user.paddle_updated_at and checkout.created_at > user.paddle_updated_at)
    other = bool(user.provider_subscription_id) and not same and not resubscribe
    if existing or other:
        db.get(PaddleEvent, event["event_id"]).details = {"checkout_id": checkout.id,
            "subscription_id": subscription_id, "transaction_id": checkout.transaction_id, "status": status}
        if decision == "grant":
            checkout.subscription_id = subscription_id
            return record_conflict(db, user, "paddle", subscription_id, checkout.id,
                                   checkout.transaction_id, event["event_id"])
        return "conflict_lifecycle_recorded" if existing else (
            "ignored_other_provider" if user.payment_provider != "paddle" else "ignored_old_or_second_subscription")
    # Unpaid/trial/canceled attempts must not claim the primary provider slot.
    if not same and decision != "grant":
        checkout.subscription_id = subscription_id
        return "recorded_unpaid"
    if same and user.paddle_updated_at and occurred <= user.paddle_updated_at:
        return "ignored_stale"
    change = data.get("scheduled_change") or {}
    cancel_at = utc_date(change["effective_at"]) if change.get("action") == "cancel" else None
    apply_subscription_access(db, user, decision, period_start=start, period_end=end, scheduled_cancel_at=cancel_at)
    checkout.subscription_id, checkout.state = subscription_id, "completed"
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
        if db.get(PaddleEvent, event_id):
            db.rollback()
            return "duplicate"
        if event_type in EVENTS:
            data = event["data"]
            checkout = checkout_for_event(db, data)
            owner = db.get(User, checkout.user_id)
            items = data.get("items") or []
            eligible = len(items) == 1 and items[0].get("quantity") == 1 and items[0].get("price", {}).get("id") == premium_price_id()
            needs_confirmation = data.get("status") == "active" and eligible and not (
                owner.payment_provider == "paddle" and owner.provider_subscription_id == data["id"]
                and owner.current_period_start is not None)
            saved = (checkout.transaction_id, data["id"], data["customer_id"], checkout.id, checkout.user_id)
            db.rollback()
            if needs_confirmation:
                confirm_initial_payment(*saved)
            # Same locking order as Lava: user -> event ledger -> conflict.
            locked_billing_user(db, saved[-1])
        insert = pg_insert if db.bind.dialect.name == "postgresql" else sqlite_insert
        result = db.execute(insert(PaddleEvent).values(event_id=event_id, event_type=event_type,
            provider="paddle", occurred_at=occurred, processed_at=datetime.utcnow(), outcome="processed"
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
