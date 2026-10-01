"""Lava adapter. Provider HTTP calls precede short, User-locked transactions."""
import hashlib
import hmac
import json
import os
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from types import SimpleNamespace

from fastapi import HTTPException
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from database.queries import User, PaddleCheckout, PaddleEvent, LavaCheckout
from modules import lava_client as api
from modules.subscription_access import apply_subscription_access
from modules.usage import locked_user
from modules.plans import effective_plan
from modules.billing_conflicts import conflict_for, record_conflict, locked_billing_user

EVENTS = {"payment.success", "payment.failed", "subscription.recurring.payment.success",
          "subscription.recurring.payment.failed", "subscription.cancelled", "refund.success", "chargeback.initiated"}
FINANCIAL = {"refund.success", "chargeback.initiated"}


def owns_checkout(db, user, checkout):
    # Historical superseded/abandoned attempts are still payable externally.
    if user.payment_provider not in (None, "lava"):
        return False
    if user.provider_subscription_id in (None, checkout.contract_id):
        return True
    # Same-provider resubscribe is allowed only from a new server checkout after
    # the old access ended; completed/late old contracts cannot reclaim ownership.
    primary = db.query(LavaCheckout).filter_by(user_id=user.id, contract_id=user.provider_subscription_id).first()
    return (checkout.state == "ready" and effective_plan(user) == "free"
            and user.subscription_status in ("canceled", "failed") and primary is not None
            and primary.updated_at is not None and checkout.created_at > primary.updated_at)


def email(value):
    if not isinstance(value, str) or "@" not in value or len(value) > 320:
        raise ValueError("Invalid buyer")
    return value


def utc(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("Timezone required")
    return result.astimezone(timezone.utc).replace(tzinfo=None)


def verify(headers):
    secret = os.getenv("LAVA_WEBHOOK_SECRET", "")
    if not 32 <= len(secret) <= 80:
        raise HTTPException(503, "Webhook is not configured")
    if not hmac.compare_digest(headers.get("x-api-key", "").encode(), secret.encode()):
        raise HTTPException(401, "Invalid webhook authentication")


def reconcile_checkout(db, user_id):
    """Refresh this provider's attempt before reuse. Other providers stay free."""
    user = locked_user(db, user_id)
    if user.plan == "premium" or user.subscription_status in ("active", "past_due", "paused", "trialing"):
        db.rollback()
        return
    checkout = db.query(LavaCheckout).filter_by(user_id=user_id).filter(
        LavaCheckout.state.in_(("creating", "ready", "failed"))).order_by(LavaCheckout.created_at.desc()).first()
    if not checkout:
        db.rollback()
        return
    binding, contract, buyer = checkout.id, checkout.contract_id, checkout.buyer_email
    db.rollback()
    if not contract:
        raise HTTPException(409, "Lava ещё не подтвердила создание оплаты. Повторная оплата пока недоступна.")
    invoice = api.request("GET", "/api/v2/invoices/" + contract)
    try:
        if api.external_id(invoice["id"]) != contract or invoice["buyer"]["email"] != buyer:
            raise ValueError()
        status = invoice["status"]
        if status not in ("NEW", "IN_PROGRESS", "COMPLETED", "FAILED"):
            raise ValueError()
        state = snapshot(SimpleNamespace(contract_id=contract, buyer_email=buyer)) if status == "FAILED" else None
    except (ValueError, KeyError, TypeError, AttributeError, InvalidOperation):
        raise HTTPException(502, "Не удалось проверить состояние оплаты Lava. Попробуйте позже.") from None
    user = locked_user(db, user_id)
    checkout = db.get(LavaCheckout, binding)
    db.refresh(checkout)
    if user.plan == "premium" or checkout.paid_at or checkout.state == "completed":
        db.rollback()
        raise HTTPException(409, "Подписка уже оплачена. Обновите лимиты аккаунта.")
    if checkout.state not in ("creating", "ready", "failed"):
        db.rollback()
        return
    if status == "FAILED" and state and not state["paid"] and state["status"] == "FAILED":
        checkout.state = "failed"
        # Older deployments assigned billing ownership even for an unpaid failure.
        if (user.payment_provider == "lava" and user.provider_subscription_id == contract
                and user.subscription_status == "failed"):
            user.payment_provider = user.provider_subscription_id = user.provider_customer_id = None
            user.subscription_status = None
        db.commit()
        return
    db.rollback()
    if status == "COMPLETED" or (state and state["paid"]):
        raise HTTPException(409, "Оплата Lava подтверждается. Обновите лимиты аккаунта перед новой покупкой.")
    if checkout.state == "failed" or status == "FAILED":
        raise HTTPException(409, "Lava ещё подтверждает результат оплаты. Попробуйте снова позже.")


def create_checkout(db, user_id):
    offer, returns = api.config()
    try:
        reconcile_checkout(db, user_id)
        user = locked_user(db, user_id)
        if user.payment_provider not in (None, "lava") or user.plan == "premium" or user.subscription_status in ("active", "past_due", "paused", "trialing"):
            raise HTTPException(409, "У аккаунта уже есть подписка. Смена провайдера пока недоступна.")
        old = db.query(LavaCheckout).filter_by(user_id=user_id).order_by(LavaCheckout.created_at.desc()).first()
        if old and old.state in ("creating", "ready"):
            if not old.payment_url:
                raise HTTPException(409, "Создание оплаты ожидает проверки. Обратитесь в поддержку перед повторной оплатой.")
            db.commit()
            return {"url": old.payment_url}
        email = user.email
        db.rollback()
        product = api.verified_offer(offer)
        # Recheck while locked after catalog HTTP; simultaneous checkouts serialize.
        user = locked_user(db, user_id)
        if user.payment_provider not in (None, "lava") or user.plan == "premium" or user.subscription_status in ("active", "past_due", "paused", "trialing"):
            raise HTTPException(409, "У аккаунта уже есть подписка.")
        if db.query(LavaCheckout).filter_by(user_id=user_id).filter(LavaCheckout.state.in_(("creating", "ready"))).first():
            raise HTTPException(409, "Оплата уже создаётся. Обновите страницу.")
        checkout = LavaCheckout(user_id=user_id, offer_id=offer, product_id=product, buyer_email=email)
        db.add(checkout); db.flush()
        binding = checkout.id
        db.commit()  # Keep ambiguous attempts blocked; never retry invoice POST automatically.
        response = api.request("POST", "/api/v3/invoice", json={"email": email, "offerId": offer,
            "currency": "RUB", "periodicity": "MONTHLY", "buyerLanguage": "RU", **returns})
        contract = api.external_id(response["id"])
        url = api.https_url(response["paymentUrl"])
        if response.get("amountTotal", {}).get("currency") != "RUB" or Decimal(str(response["amountTotal"]["amount"])) != api.PREMIUM_RUB:
            raise ValueError("Unexpected invoice amount")
        locked_user(db, user_id)
        checkout = db.get(LavaCheckout, binding)
        checkout.contract_id, checkout.payment_url, checkout.state = contract, url, "ready"
        db.commit()
        return {"url": url}
    except HTTPException:
        db.rollback(); raise
    except (ValueError, KeyError, TypeError, AttributeError, InvalidOperation):
        db.rollback()
        raise HTTPException(502, "Оплату нужно проверить. Обратитесь в поддержку перед повторной оплатой.") from None


def parse_event(body):
    if not isinstance(body, dict):
        raise ValueError()
    kind = body.get("event_type") or body.get("eventType")
    if not isinstance(kind, str):
        raise ValueError()
    if kind not in EVENTS:
        return None
    if kind in FINANCIAL:
        data = body["data"]
        event_id = api.external_id(body["event_id"])
        at = utc(body["created_at"])
        if type(data["subscription_cancelled"]) is not bool:
            raise ValueError()
        if kind == "refund.success" and data["refund_type"] not in ("full", "partial"):
            raise ValueError()
        amount = Decimal(str(data["amount"]))
        if not amount.is_finite() or amount <= 0 or data["currency"] != "RUB":
            raise ValueError()
        details = {k: data[k] for k in ("amount", "currency", "subscription_cancelled")}
        details["reference_id"] = api.external_id(data["refund_id" if kind == "refund.success" else "chargeback_id"])
        if kind == "refund.success":
            details["refund_type"] = data["refund_type"]
        else:
            details["dispute_date"] = data["dispute_date"]
        return dict(kind=kind, key=event_id, at=at, data=data, details=details,
                    product=api.external_id(data["product"]["product_id"]),
                    offer=api.external_id(data["product"]["tier_id"]), email=email(data["customer_email"]))
    at = utc(body["cancelledAt"] if kind == "subscription.cancelled" else body["timestamp"])
    contract = api.external_id(body["contractId"])
    root = api.external_id(body["parentContractId"]) if body.get("parentContractId") else contract
    if kind == "subscription.cancelled":
        utc(body["willExpireAt"])
    else:
        expected = "subscription-active" if kind.endswith(".success") else "subscription-failed"
        if body["status"] != expected or body["currency"] != "RUB" or Decimal(str(body["amount"])) != api.PREMIUM_RUB:
            raise ValueError()
    # Documented immutable contract/event/time fields; exclude error text and delivery details.
    key = hashlib.sha256(json.dumps([kind, contract, root, at.isoformat()], separators=(",", ":")).encode()).hexdigest()
    return dict(kind=kind, key=key, at=at, data=body, details=None, root=root, contract=contract,
                product=api.external_id(body["product"]["id"]), email=email(body["buyer"]["email"]))


def snapshot(checkout):
    value = api.request("GET", "/api/v1/subscriptions/" + checkout.contract_id)
    if api.external_id(value["id"]) != checkout.contract_id or value["buyer"]["email"] != checkout.buyer_email or value["periodicity"] != "MONTHLY":
        raise ValueError("Subscription binding mismatch")
    if value["subscriptionStatus"] not in ("ACTIVE", "CANCELLED", "FAILED"):
        raise ValueError()
    # API supplies exact expiry; never synthesize a month by adding 30 days.
    end = utc(value["expiredAt"]) if value.get("expiredAt") else None
    cancelled = utc(value["cancelledAt"]) if value.get("cancelledAt") else None
    terminated = utc(value["terminatedAt"]) if value.get("terminatedAt") else None
    payments = []
    completed_ids = set()
    if value["status"] == "COMPLETED":
        payments.append((utc(value["datetime"]), value["receipt"]))
        completed_ids.add(checkout.contract_id)
    for payment in value.get("recurrentPayments") or []:
        if payment["status"] == "COMPLETED":
            payments.append((utc(payment["datetime"]), payment))
            completed_ids.add(api.external_id(payment["id"]))
    paid = max(payments, key=lambda p: p[0]) if payments else None
    if paid and (Decimal(str(paid[1]["amount"])) != api.PREMIUM_RUB or paid[1]["currency"] != "RUB"):
        raise ValueError("Unexpected paid amount")
    return dict(paid=paid[0] if paid else None, end=end, cancelled=cancelled,
                terminated=terminated, status=value["subscriptionStatus"], completed_ids=completed_ids)


def apply_snapshot(db, user, checkout, state):
    paid, end = state["paid"], state["end"]
    if checkout.paid_at and (not paid or paid < checkout.paid_at):
        return "ignored_stale_snapshot"
    if not paid:
        checkout.state = "failed"
        if user.payment_provider == "lava" and user.provider_subscription_id == checkout.contract_id:
            user.payment_provider = user.provider_customer_id = user.provider_subscription_id = None
            user.subscription_status = None
        return "processed"
    # An older ACTIVE snapshot fetched concurrently must not undo cancellation.
    if ((user.scheduled_cancel_at or user.subscription_status == "canceled") and paid == checkout.paid_at and not state["cancelled"]
            and not state["terminated"] and state["status"] == "ACTIVE"):
        return "ignored_stale_snapshot"
    if state["terminated"]:
        apply_subscription_access(db, user, "revoke")
        user.subscription_status = "canceled"
    elif paid and end and paid < end:
        if checkout.paid_at and paid < checkout.paid_at:
            return "ignored_stale_snapshot"
        cancel_at = end if state["status"] == "CANCELLED" or state["cancelled"] else None
        apply_subscription_access(db, user, "grant", period_start=paid, period_end=end,
                                  scheduled_cancel_at=cancel_at)
        user.subscription_status = "canceled" if cancel_at else "past_due" if state["status"] == "FAILED" else "active"
        checkout.paid_at = paid
    elif paid:
        raise ValueError("Missing paid period")
    # Lava does not expose a customer ID; email is a cross-check, never identity.
    user.payment_provider = "lava"
    user.provider_customer_id = None
    user.provider_subscription_id = checkout.contract_id
    checkout.state = "completed" if paid else "failed"
    return "processed"


def process_event(db, body):
    try:
        event = parse_event(body)
        if event is None:
            return "ignored_event_type"
        # Namespace only new provider IDs. Legacy Paddle evt_* IDs and its old
        # ON CONFLICT(event_id) remain compatible across migration/deployment.
        event["key"] = "lava:" + event["key"]
        if db.get(PaddleEvent, event["key"]):
            db.rollback(); return "duplicate"
        if event["kind"] in FINANCIAL:
            candidates = db.query(LavaCheckout).filter_by(offer_id=event["offer"], product_id=event["product"],
                                                         buyer_email=event["email"]).all()
            # There is no contract ID in this envelope. Only reconcile a single
            # locally bound subscription, and only from its authenticated API state.
            candidates = [c for c in candidates if c.contract_id and c.created_at <= event["at"]]
            checkout = candidates[0] if len(candidates) == 1 else None
        else:
            checkout = db.query(LavaCheckout).filter_by(contract_id=event["root"]).first()
            if not checkout and event["kind"] == "subscription.cancelled":
                db.rollback()
                invoice = api.request("GET", "/api/v2/invoices/" + event["contract"])
                root = api.external_id((invoice.get("parentInvoice") or {}).get("id", event["contract"]))
                checkout = db.query(LavaCheckout).filter_by(contract_id=root).first()
            if not checkout:
                # Includes webhook-before-checkout-response race: retry, do not bind by email.
                raise HTTPException(503, "Payment binding is not available yet")
            if checkout.product_id != event["product"] or checkout.buyer_email != event["email"]:
                raise ValueError("Checkout mismatch")
        binding, user_id = (checkout.id, checkout.user_id) if checkout else (None, None)
        state = None
        if checkout:
            user = db.get(User, user_id)
            allowed = owns_checkout(db, user, checkout)
            needs_state = event["kind"] not in FINANCIAL or event["data"]["subscription_cancelled"]
            db.rollback()
            # A second paid subscription must be verified and recorded, never
            # silently ignored merely because another provider won the race.
            success = event["kind"] in ("payment.success", "subscription.recurring.payment.success")
            if (allowed or success) and needs_state:
                # Load scalar binding before ending read transaction; HTTP has no DB lock.
                checkout = db.get(LavaCheckout, binding)
                saved = SimpleNamespace(contract_id=checkout.contract_id, buyer_email=checkout.buyer_email)
                db.rollback()
                state = snapshot(saved)
                if event["kind"] not in FINANCIAL:
                    if event["kind"] == "payment.failed" and not state["paid"] and state["status"] != "FAILED":
                        raise HTTPException(503, "Payment failure confirmation is pending")
                    if event["kind"].endswith(".success") and event["contract"] not in state["completed_ids"]:
                        raise HTTPException(503, "Payment confirmation is pending")
                    if success and (not state["paid"] or not state["end"] or state["paid"] >= state["end"]):
                        raise ValueError("Missing paid period")
                    if event["kind"] == "subscription.cancelled" and not (state["cancelled"] or state["terminated"] or state["status"] == "CANCELLED"):
                        raise HTTPException(503, "Cancellation confirmation is pending")
        db.rollback()
        user = locked_billing_user(db, user_id) if user_id else None
        insert = pg_insert if db.bind.dialect.name == "postgresql" else sqlite_insert
        result = db.execute(insert(PaddleEvent).values(provider="lava", event_id=event["key"],
            event_type=event["kind"], occurred_at=event["at"], processed_at=datetime.utcnow(),
            details=event["details"], outcome="processed").on_conflict_do_nothing(index_elements=["event_id"]))
        if result.rowcount == 0:
            db.commit(); return "duplicate"
        outcome = "recorded_unmatched" if not binding else "recorded"
        if binding:
            checkout = db.get(LavaCheckout, binding)
            db.refresh(checkout)
            existing_conflict = conflict_for(db, "lava", checkout.contract_id)
            if existing_conflict or not owns_checkout(db, user, checkout):
                db.get(PaddleEvent, event["key"]).details = {**(event["details"] or {}),
                    "checkout_id": checkout.id, "subscription_id": checkout.contract_id,
                    "payment_id": event.get("contract")}
                if event["kind"] in ("payment.success", "subscription.recurring.payment.success") and state:
                    outcome = record_conflict(db, user, "lava", checkout.contract_id, checkout.id,
                                              event["contract"], event["key"])
                else:
                    outcome = "conflict_lifecycle_recorded" if existing_conflict else "ignored_other_subscription"
            elif event["kind"] in FINANCIAL:
                if event["data"]["subscription_cancelled"] and state:
                    if state["cancelled"] or state["terminated"] or state["status"] == "CANCELLED":
                        outcome = apply_snapshot(db, user, checkout, state)
                        times = [v for v in (checkout.updated_at, state["cancelled"], state["terminated"], state["paid"]) if v]
                        checkout.updated_at = max(times) if times else checkout.updated_at
                    else:
                        # API has not caught up, so keep delivery retryable.
                        raise HTTPException(503, "Cancellation confirmation is pending")
            elif checkout.updated_at and event["at"] < checkout.updated_at:
                outcome = "ignored_stale"
            elif state:
                outcome = apply_snapshot(db, user, checkout, state)
                checkout.updated_at = max(filter(None, (checkout.updated_at, event["at"], state["paid"], state["cancelled"], state["terminated"])))
        db.get(PaddleEvent, event["key"]).outcome = outcome
        db.commit()
        return outcome
    except HTTPException:
        db.rollback(); raise
    except (ValueError, KeyError, TypeError, AttributeError, InvalidOperation):
        db.rollback()
        raise HTTPException(400, "Invalid or unverified Lava event") from None
    except Exception:
        db.rollback(); raise


def cancel_subscription(db, user_id):
    try:
        user = locked_user(db, user_id)
        if user.payment_provider != "lava" or not user.provider_subscription_id:
            raise HTTPException(409, "Нет подписки Lava для этого аккаунта.")
        checkout = db.query(LavaCheckout).filter_by(user_id=user_id, contract_id=user.provider_subscription_id).one()
        if checkout.cancel_requested or user.scheduled_cancel_at:
            db.rollback(); return {"pending": True}
        contract, email = checkout.contract_id, checkout.buyer_email
        # Avoid repeating an ambiguous destructive network request automatically.
        checkout.cancel_requested = True
        db.commit()
        api.request("DELETE", "/api/v1/subscriptions", params={"contractId": contract, "email": email})
        # Only the verified cancellation lifecycle supplies the actual expiry.
        return {"pending": True}
    except HTTPException:
        db.rollback(); raise
