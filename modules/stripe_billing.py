"""Stripe Billing. Signed events + verified paid invoices own access, never the browser.

Provider reads run outside user locks. A checkout revision protects snapshots fetched
concurrently; a losing reconciliation is retried instead of applying stale state.
All event, period, conflict and entitlement writes commit in one transaction.
"""
import os
import json
import logging
import re
import time
from datetime import datetime, timedelta
from urllib.parse import urlsplit

import stripe
from fastapi import HTTPException
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from database.queries import User, StripeCheckout, BillingCustomer, BillingEvent, BillingPeriod, QuestionPack
from modules import offers
from modules.billing_conflicts import locked_billing_user, release_legacy_unpaid_owner, conflict_for, record_conflict
from modules.subscription_access import apply_subscription_access
from modules.plans import effective_plan
from modules.product_analytics import capture_payment_failure

logger = logging.getLogger(__name__)

# Refusal texts. While a payment outcome is unknown, never invite another attempt.
CHECKOUT_PENDING = ("Проверяем оплату. Не оплачивайте повторно — обновите страницу через минуту. "
                    "Если сообщение повторится, напишите в поддержку: lunariaai.support@gmail.com.")
CHECKOUT_UNKNOWN = ("Не удалось открыть или проверить оплату. Не оплачивайте повторно — обновите страницу через минуту. "
                    "Если ошибка повторится, напишите в поддержку: lunariaai.support@gmail.com.")
CHECKOUT_MANUAL = ("Прошлую оплату нужно проверить вручную. Не оплачивайте повторно — напишите в поддержку: "
                   "lunariaai.support@gmail.com.")
STRIPE_UNAVAILABLE = "Stripe сейчас недоступен. Попробуйте позже."
HAS_PREMIUM_YEAR = ("У вас уже есть Premium. Годовой тариф можно оформить, когда закончится текущий период: "
                    "отключите продление в «Мои карты».")


def log_checkout_failure(stage, error):
    # No provider messages, request bodies, emails, keys, or client secrets.
    diagnostic = {"stage": stage, "exception_class": type(error).__name__}
    if isinstance(error, stripe.StripeError):
        diagnostic["http_status"] = error.http_status
        for name, value, pattern in (
            ("code", error.code, r"[a-z_]{1,64}"),
            ("param", getattr(error, "param", None), r"[a-z_\[\]0-9]{1,100}"),
            ("request_id", error.request_id, r"req_[A-Za-z0-9]{1,80}"),
        ):
            if isinstance(value, str) and re.fullmatch(pattern, value):
                diagnostic[name] = value
    logger.warning("Stripe checkout failed %s", json.dumps(diagnostic))

API_VERSION = "2026-09-30.endive"
EVENTS = {
    "checkout.session.completed", "checkout.session.expired",
    "checkout.session.async_payment_succeeded", "checkout.session.async_payment_failed",
    "invoice.paid", "invoice.payment_failed", "invoice.payment_action_required", "invoice.finalization_failed",
    "customer.subscription.created", "customer.subscription.updated", "customer.subscription.deleted",
    "refund.created", "refund.updated", "refund.failed", "charge.refunded",
    "charge.dispute.created", "charge.dispute.closed",
}
FINANCIAL = {v for v in EVENTS if v.startswith(("refund.", "charge."))}


def ident(value, prefix):
    if isinstance(value, stripe.StripeObject): value = value.to_dict(recursive=True)
    value = value.get("id") if hasattr(value, "get") else value
    if not isinstance(value, str) or not re.fullmatch(prefix + r"_[A-Za-z0-9_]+", value):
        raise ValueError("Invalid identifier")
    return value


def date(value):
    if type(value) not in (int, float) or value <= 0:
        raise ValueError("Invalid timestamp")
    return datetime.utcfromtimestamp(value)


def livemode():
    """Exactly "true" is production; exactly "false" is the loopback test stand; anything else refuses."""
    value = os.getenv("STRIPE_LIVEMODE")
    if value not in ("true", "false"):
        raise HTTPException(503, STRIPE_UNAVAILABLE)
    return value == "true"


def config():
    key = os.getenv("STRIPE_SECRET_KEY", "")
    account = os.getenv("STRIPE_ACCOUNT_ID", "")
    price = os.getenv("STRIPE_PREMIUM_PRICE_ID", "")
    keys = ("sk_live_", "rk_live_") if livemode() else ("sk_test_",)
    if (not key.startswith(keys)
            or not re.fullmatch(r"acct_[A-Za-z0-9]+", account)
            or not re.fullmatch(r"price_[A-Za-z0-9]+", price)
            or os.getenv("STRIPE_API_VERSION", API_VERSION) != API_VERSION):
        raise HTTPException(503, STRIPE_UNAVAILABLE)
    return account, price


def configured():
    try:
        config()
        return os.getenv("STRIPE_CHECKOUT_ENABLED", "false") == "true"
    except HTTPException:
        return False


def return_url(name):
    value = os.getenv(name, "")
    parts = urlsplit(value)
    try:
        port = parts.port
    except ValueError:
        raise HTTPException(503, STRIPE_UNAVAILABLE) from None
    if livemode():
        allowed = parts.scheme == "https" and parts.hostname == "mylunariaai.com" and not port
    else:  # the test stand returns only to the local frontend
        allowed = parts.scheme == "http" and parts.hostname in ("localhost", "127.0.0.1") and port == 3000
    if not allowed or parts.username or parts.password:
        raise HTTPException(503, STRIPE_UNAVAILABLE)
    return value


def api():
    account, _ = config()
    client = stripe.StripeClient(os.environ["STRIPE_SECRET_KEY"], stripe_version=API_VERSION,
        max_network_retries=0, http_client=stripe.RequestsClient(timeout=10))
    try:
        actual = client.raw_request("get", "/v1/account").data
        if actual["id"] != account:
            raise HTTPException(503, STRIPE_UNAVAILABLE)
    except stripe.StripeError:
        raise HTTPException(503, STRIPE_UNAVAILABLE) from None
    return client.v1


def checked(value):
    if isinstance(value, stripe.StripeObject): value = value.to_dict(recursive=True)
    if value.get("livemode") is not livemode():
        raise ValueError("Wrong payment environment")
    return value


def verify_signature(body, headers):
    secret = os.getenv("STRIPE_WEBHOOK_SECRET", "")
    if not secret:
        raise HTTPException(503, "Stripe webhook is not configured")
    try:
        signature = headers.get("stripe-signature", "")
        timestamp = int(next(p[2:] for p in signature.split(",") if p.startswith("t=")))
        if abs(time.time() - timestamp) > 300:
            raise ValueError()
        stripe.Webhook.construct_event(body, signature, secret, tolerance=300)
        return json.loads(body)
    except (ValueError, TypeError, StopIteration, UnicodeError, stripe.SignatureVerificationError):
        raise HTTPException(401, "Invalid Stripe signature") from None


def eligible_checkout(db, user, product="premium_month"):
    release_legacy_unpaid_owner(db, user)
    if user.payment_provider not in (None, "stripe"):
        raise HTTPException(409, ("Подписка этого аккаунта оформлена через Lava.top, поэтому оплата через Stripe недоступна. "
                                 "Чтобы сменить способ оплаты, напишите в поддержку: lunariaai.support@gmail.com."))
    if product == "premium_year" and effective_plan(user) == "premium":
        raise HTTPException(409, HAS_PREMIUM_YEAR)
    if effective_plan(user) == "premium" or user.subscription_status in ("active", "past_due", "paused", "trialing", "unpaid", "incomplete"):
        raise HTTPException(409, ("У аккаунта уже есть подписка или незавершённая оплата. Не оплачивайте повторно — "
                                 "обновите страницу через минуту. Если Premium не появится, напишите в поддержку: "
                                 "lunariaai.support@gmail.com."))
    if user.provider_subscription_id and user.subscription_status != "canceled":
        raise HTTPException(409, CHECKOUT_MANUAL)


def price_id(product):
    """Server price table: the month from its existing variable, new options from theirs (None if unset)."""
    return config()[1] if product == "premium_month" else offers.option_id("stripe", product)


def validate_price(value, *, checkout=True, product="premium_month"):
    try:
        value = checked(value)
    except ValueError:
        if checkout:  # a price from the other mode cannot be sold here
            raise HTTPException(503, STRIPE_UNAVAILABLE) from None
        raise
    recurring = value.get("recurring") or {}
    interval = offers.INTERVALS.get(product)
    if interval:
        shape = (recurring.get("interval") == interval and recurring.get("interval_count") == 1
                 and recurring.get("usage_type") == "licensed")
    else:
        shape = value.get("type") == "one_time"
    if (value["id"] != price_id(product) or (checkout and not value.get("active")) or value.get("currency") != "usd"
            or value.get("unit_amount") != offers.STRIPE_CENTS[product] or not shape):
        raise HTTPException(503, STRIPE_UNAVAILABLE)


def subscription_product(intent_price):
    # stripe_checkouts.price_id NULL is the monthly price (rows before PRICING-1 included).
    return "premium_month" if intent_price is None else "premium_year"


def session_response(session):
    session = checked(session)
    if session.get("status") != "open" or not session.get("client_secret"):
        raise HTTPException(409, CHECKOUT_PENDING)
    return {"session_id": session["id"], "client_secret": session["client_secret"]}


def customer_row(db, user_id, account, live):
    """Caller holds the user lock. One scoped customer binding per user, account and mode."""
    customer = db.query(BillingCustomer).filter_by(user_id=user_id, provider="stripe", account_id=account, livemode=live).first()
    if customer is None:
        customer = BillingCustomer(user_id=user_id, provider="stripe", account_id=account, livemode=live)
        db.add(customer); db.flush()
    if not customer.customer_id and customer.created_at < datetime.utcnow() - timedelta(hours=23):
        raise HTTPException(409, CHECKOUT_MANUAL)
    return customer


def bind_remote_customer(db, client, user_id, customer_binding, email, attempt):
    """Create the Stripe customer once (stable idempotency key), then bind it to the customer and attempt rows."""
    remote = checked(client.customers.create({"email": email, "metadata": {"billing_binding": customer_binding}},
        options={"idempotency_key": "customer:" + customer_binding}))
    customer_id = ident(remote, "cus")
    locked_billing_user(db, user_id)
    customer = db.get(BillingCustomer, customer_binding)
    if customer.customer_id and customer.customer_id != customer_id:
        raise ValueError("Customer mismatch")
    customer.customer_id = customer_id
    db.get(*attempt).customer_id = customer_id
    db.commit()
    return customer_id


def create_checkout(db, user_id, product="premium_month"):
    target = offers.require("stripe", product)  # beta and unset options refuse before any provider call
    if not configured():
        raise HTTPException(503, STRIPE_UNAVAILABLE)
    if product == "question_pack":
        return create_pack_checkout(db, user_id, target)
    account, monthly = config()
    live = livemode()
    price = target or monthly
    stage = "account_read"
    try:
        client = api()
        stage = "price_read"
        validate_price(client.prices.retrieve(price), product=product)
        destination = return_url("STRIPE_CHECKOUT_RETURN_URL")
        stage = "intent_bind"
        user = locked_billing_user(db, user_id)
        eligible_checkout(db, user, product)
        intent = db.query(StripeCheckout).filter_by(user_id=user_id, account_id=account, livemode=live).filter(
            StripeCheckout.state.in_(("creating", "ready"))).order_by(StripeCheckout.created_at.desc()).first()
        if intent and intent.session_id:
            saved_id, other = intent.session_id, intent.price_id != target
            db.rollback()
            stage = "session_read"
            session = checked(client.checkout.sessions.retrieve(saved_id))
            if other and session.get("status") == "open":
                # Switching the plan: close the open session once; an unclear outcome is re-read on the next click.
                stage = "session_expire"
                session = checked(client.checkout.sessions.expire(saved_id, options={"idempotency_key": "expire:" + saved_id}))
            if session.get("status") != "expired":
                if other:
                    raise HTTPException(409, CHECKOUT_PENDING)
                return session_response(session)
            user = locked_billing_user(db, user_id)
            eligible_checkout(db, user, product)
            intent = db.query(StripeCheckout).filter_by(session_id=saved_id).one()
            if intent.state != "ready" or intent.subscription_id:
                raise HTTPException(409, CHECKOUT_PENDING)
            intent.state = "expired"
            db.flush()
            intent = None
        if intent and intent.price_id != target:
            # shortcut: an unanswered attempt for the other plan blocks the switch; its own plan recovers it.
            raise HTTPException(409, CHECKOUT_PENDING)
        if intent and intent.created_at < datetime.utcnow() - timedelta(hours=23):
            # Stripe retains idempotency keys for at least 24h, not indefinitely.
            raise HTTPException(409, CHECKOUT_MANUAL)
        customer = customer_row(db, user_id, account, live)
        if intent is None:
            intent = StripeCheckout(user_id=user_id, account_id=account, livemode=live, price_id=target)
            db.add(intent); db.flush()
        binding, customer_binding, customer_id, email = intent.id, customer.id, customer.customer_id, user.email
        db.commit()
        if not customer_id:
            stage = "customer_create"
            customer_id = bind_remote_customer(db, client, user_id, customer_binding, email, (StripeCheckout, binding))
        metadata = {"checkout_binding": binding, "user_id": str(user_id)}
        params = {"mode": "subscription", "ui_mode": "embedded_page", "customer": customer_id,
            # The pinned API uses Dashboard-managed dynamic payment methods.
            "line_items": [{"price": price, "quantity": 1}],
            "redirect_on_completion": "if_required", "return_url": destination,
            "metadata": metadata, "client_reference_id": binding,
            "subscription_data": {"metadata": metadata, "billing_mode": {"type": "flexible"}}}
        stage = "session_create"
        session = checked(client.checkout.sessions.create(params, options={"idempotency_key": "checkout:" + binding}))
        stage = "session_bind"
        locked_billing_user(db, user_id)
        intent = db.get(StripeCheckout, binding)
        if intent.session_id and intent.session_id != session["id"]:
            raise ValueError("Session mismatch")
        intent.customer_id = customer_id
        intent.session_id = ident(session, "cs")
        intent.expires_at = date(session["expires_at"])
        if intent.state == "creating": intent.state = "ready"
        db.commit()
        return session_response(session)
    except HTTPException:
        db.rollback(); raise
    except Exception as error:
        db.rollback()
        log_checkout_failure(stage, error)
        raise HTTPException(502, CHECKOUT_UNKNOWN) from None


def create_pack_checkout(db, user_id, price):
    """One-time Checkout for a question pack. Never touches subscription ownership; one open pack per user."""
    account, _ = config()
    live = livemode()
    stage = "account_read"
    try:
        client = api()
        stage = "price_read"
        validate_price(client.prices.retrieve(price), product="question_pack")
        destination = return_url("STRIPE_CHECKOUT_RETURN_URL")
        stage = "pack_bind"
        user = locked_billing_user(db, user_id)
        pack = db.query(QuestionPack).filter_by(user_id=user_id, provider="stripe", account_id=account, livemode=live).filter(
            QuestionPack.state.in_(("creating", "ready"))).order_by(QuestionPack.created_at.desc()).first()
        if pack and pack.session_id:
            saved_id = pack.session_id
            db.rollback()
            stage = "session_read"
            session = checked(client.checkout.sessions.retrieve(saved_id))
            if session.get("status") == "open":
                return session_response(session)
            if session.get("status") != "expired":
                raise HTTPException(409, offers.PACK_PENDING)
            user = locked_billing_user(db, user_id)
            pack = db.query(QuestionPack).filter_by(session_id=saved_id).one()
            if pack.state != "ready":
                raise HTTPException(409, offers.PACK_PENDING)
            pack.state = "expired"
            db.flush()
            pack = None
        if pack and pack.created_at < datetime.utcnow() - timedelta(hours=23):
            raise HTTPException(409, CHECKOUT_MANUAL)
        customer = customer_row(db, user_id, account, live)
        if pack is None:
            pack = QuestionPack(user_id=user_id, provider="stripe", account_id=account, livemode=live,
                                quantity=offers.PACK_SIZE, state="creating", customer_id=customer.customer_id)
            db.add(pack); db.flush()
        pack_id, customer_binding, customer_id, email = pack.id, customer.id, customer.customer_id, user.email
        db.commit()
        if not customer_id:
            stage = "customer_create"
            customer_id = bind_remote_customer(db, client, user_id, customer_binding, email, (QuestionPack, pack_id))
        metadata = {"pack_id": pack_id, "user_id": str(user_id)}
        params = {"mode": "payment", "ui_mode": "embedded_page", "customer": customer_id,
            "line_items": [{"price": price, "quantity": 1}],
            "redirect_on_completion": "if_required", "return_url": destination,
            "metadata": metadata, "client_reference_id": pack_id,
            # A refund or dispute that arrives before the grant still finds its pack.
            "payment_intent_data": {"metadata": metadata}}
        stage = "session_create"
        session = checked(client.checkout.sessions.create(params, options={"idempotency_key": "pack:" + pack_id}))
        stage = "session_bind"
        locked_billing_user(db, user_id)
        pack = db.get(QuestionPack, pack_id)
        if pack.session_id and pack.session_id != session["id"]:
            raise ValueError("Session mismatch")
        pack.customer_id = customer_id
        pack.session_id = ident(session, "cs")
        pack.expires_at = date(session["expires_at"])
        if pack.state == "creating": pack.state = "ready"
        db.commit()
        return session_response(session)
    except HTTPException:
        db.rollback(); raise
    except Exception as error:
        db.rollback()
        log_checkout_failure(stage, error)
        raise HTTPException(502, CHECKOUT_UNKNOWN) from None


def checkout_status(db, user_id, session_id):
    account, _ = config()
    live = livemode()
    intent = db.query(StripeCheckout).filter_by(user_id=user_id, account_id=account, livemode=live, session_id=session_id).first()
    if intent is None: raise HTTPException(404, "Оплата не найдена. Обновите страницу.")
    return {"state": intent.state}


def portal_customer_id(db, user):
    """Eligibility for the current primary subscription, never a historical customer alone."""
    if (not user or effective_plan(user) != "premium" or user.payment_provider != "stripe"
            or user.subscription_status not in ("active", "past_due")
            or not user.provider_customer_id or not user.provider_subscription_id):
        return None
    account = os.getenv("STRIPE_ACCOUNT_ID", "")
    live = os.getenv("STRIPE_LIVEMODE") != "false"  # unconfigured keeps the historical live scope
    customer = db.query(BillingCustomer).filter_by(user_id=user.id, provider="stripe",
        account_id=account, livemode=live, customer_id=user.provider_customer_id).first()
    bound = db.query(StripeCheckout).filter_by(user_id=user.id, account_id=account,
        livemode=live, customer_id=user.provider_customer_id,
        subscription_id=user.provider_subscription_id).first()
    return customer.customer_id if customer and bound else None


def create_portal(db, user_id):
    config()
    user = db.get(User, user_id)
    customer_id = portal_customer_id(db, user)
    if not customer_id:
        raise HTTPException(409, ("У аккаунта нет действующей подписки Stripe. Обновите страницу; "
                                 "если подписка оплачена, напишите в поддержку: lunariaai.support@gmail.com."))
    subscription_id = user.provider_subscription_id
    db.rollback()
    configuration = os.getenv("STRIPE_PORTAL_CONFIGURATION_ID", "")
    if not configuration.startswith("bpc_"): raise HTTPException(503, "Управление подпиской временно недоступно. Попробуйте позже.")
    try:
        client = api()
        subscription = checked(client.subscriptions.retrieve(subscription_id))
        if (ident(subscription.get("customer"), "cus") != customer_id
                or subscription.get("status") not in ("active", "past_due")):
            raise HTTPException(409, "Подписка Stripe недоступна для управления. Обновите данные аккаунта.")
        session = client.billing_portal.sessions.create({"customer": customer_id, "configuration": configuration,
            "return_url": return_url("STRIPE_PORTAL_RETURN_URL")})
        url = urlsplit(session["url"])
        if url.scheme != "https" or url.hostname != "billing.stripe.com" or url.username or url.password or url.port:
            raise ValueError()
        return {"url": session["url"]}
    except HTTPException: raise
    except Exception:
        raise HTTPException(502, "Не удалось открыть управление подпиской. Попробуйте позже.") from None


def invoice_subscription(invoice):
    return ident(invoice["parent"]["subscription_details"]["subscription"], "sub")


def list_all(service, params):
    result = []
    for _ in range(100):
        page = service.list(params)
        if isinstance(page, stripe.StripeObject): page = page.to_dict(recursive=True)
        result.extend(page["data"])
        if not page.get("has_more"): return result
        params = {**params, "starting_after": page["data"][-1]["id"]}
    raise HTTPException(503, "Payment reconciliation requires operator review")


def paid_period(client, invoice, subscription_id, customer_id, product="premium_month"):
    cents = offers.STRIPE_CENTS[product]
    invoice = checked(invoice)
    if invoice_subscription(invoice) != subscription_id or ident(invoice["customer"], "cus") != customer_id:
        raise ValueError("Invoice ownership mismatch")
    if invoice.get("status") != "paid": return None
    # No manual/out-of-band/free invoices may allocate paid Premium.
    if invoice.get("currency") != "usd" or invoice.get("amount_paid", 0) < cents or invoice.get("paid_out_of_band"):
        raise ValueError("Unexpected paid invoice")
    if invoice.get("billing_reason") not in ("subscription_create", "subscription_cycle"):
        return None
    payments = list_all(client.invoice_payments, {"invoice": invoice["id"], "status": "paid", "limit": 100})
    settled = 0
    for payment in payments:
        method = payment.get("payment") or {}
        if method.get("type") != "payment_intent": continue
        pi = checked(client.payment_intents.retrieve(ident(method.get("payment_intent"), "pi")))
        if pi.get("status") == "succeeded" and pi.get("currency") == "usd" and ident(pi.get("customer"), "cus") == customer_id:
            settled += payment.get("amount_paid", 0)
    if settled < cents: raise HTTPException(503, "Invoice payment confirmation is pending")
    lines = invoice["lines"]
    if lines.get("has_more") or len(lines["data"]) != 1:
        raise ValueError("Unexpected invoice lines")
    line = lines["data"][0]
    price = price_id(product)
    parent = line.get("parent", {}).get("subscription_item_details", {})
    if (line.get("pricing", {}).get("price_details", {}).get("price") != price
            or line.get("quantity") != 1 or parent.get("proration") is True):
        raise ValueError("Unexpected recurring line")
    start, end = date(line["period"]["start"]), date(line["period"]["end"])
    if start >= end: raise ValueError("Invalid period")
    return (ident(invoice, "in"), start, end)


def resolve(client, kind, obj):
    """Retrieve event objects; never accept ownership/amounts from browser callbacks."""
    invoice, session, financial = None, None, None
    if kind.startswith("checkout."):
        session = checked(client.checkout.sessions.retrieve(ident(obj, "cs")))
        sub_id = session.get("subscription")
        return session, None, None, None if not sub_id else ident(sub_id, "sub"), None
    if kind.startswith("customer.subscription."):
        return None, None, None, ident(obj, "sub"), None
    if kind.startswith("invoice."):
        invoice = checked(client.invoices.retrieve(ident(obj, "in")))
    else:
        if kind.startswith("refund."):
            refund = checked(client.refunds.retrieve(ident(obj, "re")))
            charge_id = refund.get("charge")
            financial = {"reference_id": refund["id"], "status": refund["status"], "amount": refund["amount"], "currency": refund["currency"]}
        elif kind.startswith("charge.dispute."):
            dispute = checked(client.disputes.retrieve(ident(obj, "dp")))
            charge_id = dispute["charge"]
            financial = {"reference_id": dispute["id"], "status": dispute["status"], "amount": dispute["amount"], "currency": dispute["currency"]}
        else:
            charge_id = ident(obj, "ch")
        charge = checked(client.charges.retrieve(ident(charge_id, "ch")))
        financial = financial or {"reference_id": charge["id"], "amount_refunded": charge["amount_refunded"], "currency": charge["currency"]}
        if not charge.get("payment_intent"): return None, None, financial, None, None
        payment_intent = ident(charge["payment_intent"], "pi")
        payments = list_all(client.invoice_payments, {"payment": {"type": "payment_intent", "payment_intent": payment_intent}, "status": "paid", "limit": 100})
        ids = {ident(p["invoice"], "in") for p in payments}
        if len(ids) != 1: return None, None, financial, None, payment_intent  # never guess ambiguous ownership
        invoice = checked(client.invoices.retrieve(ids.pop()))
    if (invoice.get("parent") or {}).get("type") != "subscription_details":
        return session, invoice, financial, None, None
    return session, invoice, financial, invoice_subscription(invoice), None


def process_event(db, event):
    user_id, owner_checked = None, False  # analytics only: confirmation failures after ownership
    try:
        account, price = config()
        live = livemode()
        checked(event)
        if event.get("account") not in (None, account) or event.get("api_version") != API_VERSION:
            raise ValueError("Wrong account or API version")
        event_id, kind = ident(event, "evt"), event["type"]
        occurred = date(event["created"])
        key = "stripe:" + account + (":live:" if live else ":test:") + event_id  # production keeps its historical keys
        if db.get(BillingEvent, key):
            db.rollback(); return "duplicate"
        db.rollback()
        if kind not in EVENTS:
            return "ignored_event_type"
        client = api()
        session, invoice, financial, sub_id, payment_intent = resolve(client, kind, event["data"]["object"])
        if session and session.get("mode") == "payment":
            return process_pack_session(db, client, key, kind, occurred, session, account, live)
        if financial and payment_intent and not sub_id:
            outcome = process_pack_financial(db, client, key, kind, occurred, financial, payment_intent, account, live)
            if outcome:
                return outcome
        if sub_id:
            # Locate locally first when possible, then read lifecycle after revision capture.
            intent = db.query(StripeCheckout).filter_by(subscription_id=sub_id, account_id=account, livemode=live).first()
            if intent is None:
                discovery = checked(client.subscriptions.retrieve(sub_id))
                binding = (discovery.get("metadata") or {}).get("checkout_binding")
                intent = db.get(StripeCheckout, binding) if isinstance(binding, str) else None
        else:
            binding = (session.get("metadata") or {}).get("checkout_binding") if session else None
            intent = db.get(StripeCheckout, binding) if binding else None
        if intent is None and sub_id:
            raise HTTPException(503, "Subscription binding unavailable; reconciliation required")
        if intent is None:
            # Non-subscription financial records are audit only.
            return record_event(db, key, kind, occurred, "recorded_unmatched", financial)
        if intent.account_id != account or intent.livemode is not live:
            raise ValueError("Checkout environment mismatch")
        binding, user_id, revision = intent.id, intent.user_id, intent.revision
        expected_customer, saved_session, saved_sub = intent.customer_id, intent.session_id, intent.subscription_id
        product = subscription_product(intent.price_id)
        db.rollback()
        if not expected_customer:
            raise HTTPException(503, "Customer binding is pending")
        sub, period = None, None
        if session:
            if (session.get("metadata", {}).get("checkout_binding") != binding
                    or session.get("metadata", {}).get("user_id") != str(user_id)
                    or ident(session["customer"], "cus") != expected_customer
                    or session.get("mode") != "subscription" or (saved_session and saved_session != session["id"])):
                raise ValueError("Session ownership mismatch")
        if sub_id:
            sub = checked(client.subscriptions.retrieve(sub_id))
            if (sub["id"] != sub_id or ident(sub["customer"], "cus") != expected_customer
                    or sub.get("metadata", {}).get("checkout_binding") != binding
                    or sub.get("metadata", {}).get("user_id") != str(user_id)
                    or (saved_sub and saved_sub != sub_id)):
                raise ValueError("Subscription ownership mismatch")
            owner_checked = True
            items = sub["items"]
            if items.get("has_more") or len(items["data"]) != 1 or items["data"][0].get("quantity") != 1:
                raise ValueError("Unexpected subscription items")
            item_price = items["data"][0]["price"]
            if item_price.get("id") in {price_id(p) for p in offers.INTERVALS} - {price_id(product)}:
                raise ValueError("Price of the other plan")  # Premium is granted only for the plan that was bought
            validate_price(item_price, checkout=False, product=product)
            if sub["status"] not in ("active", "past_due", "unpaid", "canceled", "incomplete", "incomplete_expired", "trialing", "paused"):
                raise ValueError("Unknown subscription status")
            if kind not in FINANCIAL and sub["status"] not in ("canceled", "unpaid", "paused", "incomplete_expired"):
                # Reconcile latest invoice on lifecycle events as well as invoice deliveries.
                if invoice is None and sub.get("latest_invoice"):
                    invoice = checked(client.invoices.retrieve(ident(sub["latest_invoice"], "in")))
                if invoice: period = paid_period(client, invoice, sub_id, expected_customer, product)
        user = locked_billing_user(db, user_id)
        intent = db.get(StripeCheckout, binding); db.refresh(intent)
        if db.get(BillingEvent, key):
            db.rollback(); return "duplicate"
        if intent.revision != revision:
            raise HTTPException(503, "Concurrent reconciliation; retry event")
        insert = pg_insert if db.bind.dialect.name == "postgresql" else sqlite_insert
        result = db.execute(insert(BillingEvent).values(provider="stripe", event_id=key, event_type=kind,
            occurred_at=occurred, processed_at=datetime.utcnow(), outcome="processed", details=financial
        ).on_conflict_do_nothing(index_elements=["event_id"]))
        if result.rowcount == 0: db.commit(); return "duplicate"
        intent.revision += 1
        if session:
            intent.session_id = session["id"]
            if not sub_id: intent.state = "expired" if session["status"] == "expired" else intent.state
        outcome = "recorded"
        if sub:
            release_legacy_unpaid_owner(db, user)
            intent.subscription_id, intent.subscription_status = sub_id, sub["status"]
            if sub["status"] in ("canceled", "incomplete_expired"):
                intent.ended_at = date(sub["ended_at"]) if sub.get("ended_at") else occurred
            same = user.payment_provider == "stripe" and user.provider_subscription_id == sub_id
            previous = db.query(StripeCheckout).filter_by(subscription_id=user.provider_subscription_id).first() if user.payment_provider == "stripe" else None
            resubscribe = bool(previous and previous.ended_at and intent.created_at > previous.ended_at and effective_plan(user) == "free" and user.subscription_status == "canceled")
            conflict = conflict_for(db, "stripe", sub_id)
            other = bool(user.payment_provider) and not same and not resubscribe
            row = None
            if period:
                invoice_id, start, end = period
                row = db.query(BillingPeriod).filter_by(provider="stripe", account_id=account, livemode=live, invoice_id=invoice_id).first()
                if row is None:
                    row = db.query(BillingPeriod).filter_by(provider="stripe", account_id=account, livemode=live, subscription_id=sub_id, period_start=start).first()
                if row and (row.user_id != user_id or row.subscription_id != sub_id or row.period_start != start or row.period_end != end):
                    raise ValueError("Paid period mismatch")
                if row is None:
                    row = BillingPeriod(user_id=user_id, provider="stripe", account_id=account, livemode=live,
                        invoice_id=invoice_id, subscription_id=sub_id, period_start=start, period_end=end)
                    db.add(row)
            if conflict or other:
                outcome = record_conflict(db, user, "stripe", sub_id, binding, period[0], key) if period else "conflict_lifecycle_recorded"
            elif kind in FINANCIAL:
                outcome = "financial_recorded"  # refund is not cancellation, nor new quota
            else:
                status = sub["status"]
                canceled_at = date(sub["cancel_at"]) if sub.get("cancel_at") else None
                if not canceled_at and sub.get("cancel_at_period_end"):
                    canceled_at = date(sub["items"]["data"][0]["current_period_end"])
                # A terminal subscription never resurrects from a stale paid invoice.
                if intent.ended_at and status not in ("canceled", "incomplete_expired"):
                    raise HTTPException(503, "Stale subscription snapshot")
                if same and status in ("canceled", "unpaid", "paused", "incomplete_expired"):
                    apply_subscription_access(db, user, "revoke")
                    user.subscription_status = "canceled" if status == "incomplete_expired" else status
                elif period and status == "active" and period[1] <= datetime.utcnow() < period[2]:
                    if (not row.applied or (same and user.plan != "premium")) and (not user.current_period_start or period[1] >= user.current_period_start):
                        apply_subscription_access(db, user, "grant", period_start=period[1], period_end=period[2], scheduled_cancel_at=canceled_at)
                        user.current_period_interval = offers.INTERVALS[product]
                        row.applied = True
                        user.payment_provider, user.provider_customer_id, user.provider_subscription_id = "stripe", expected_customer, sub_id
                        user.subscription_status = status
                        intent.state = "completed"
                        outcome = "granted"
                    elif same:
                        apply_subscription_access(db, user, "retain", scheduled_cancel_at=canceled_at)
                        user.subscription_status = status
                elif same:
                    apply_subscription_access(db, user, "retain", scheduled_cancel_at=canceled_at)
                    user.subscription_status = status
                if status in ("canceled", "incomplete_expired"): intent.state = "ended"
            db.get(BillingEvent, key).details = {**(financial or {}), "checkout_id": binding, "subscription_id": sub_id,
                "invoice_id": invoice["id"] if invoice else None}
        db.get(BillingEvent, key).outcome = outcome
        db.commit()
        if kind == "invoice.payment_failed" and invoice and invoice.get("status") != "paid":
            stage = {"subscription_create": "first_payment", "subscription_cycle": "renewal"}.get(invoice.get("billing_reason"))
            if stage:  # one per invoice: Stripe sends this event on every failed attempt
                capture_payment_failure(db, user_id, "stripe", stage, "payment_failed:stripe:" + invoice["id"])
        return outcome
    except HTTPException:
        db.rollback(); raise
    except (ValueError, KeyError, TypeError, AttributeError, OverflowError):
        db.rollback()
        if owner_checked and user_id:
            capture_payment_failure(db, user_id, "stripe", "confirmation", "payment_failed:confirmation:" + key)
        raise HTTPException(400, "Invalid or unverified Stripe event") from None
    except stripe.StripeError:
        db.rollback()
        raise HTTPException(503, "Stripe verification temporarily unavailable") from None
    except Exception:
        db.rollback(); raise


def stripe_pack(db, pack_id, account, live):
    pack = db.get(QuestionPack, pack_id) if isinstance(pack_id, str) else None
    if pack and (pack.provider != "stripe" or pack.account_id != account or pack.livemode is not live):
        raise ValueError("Pack environment mismatch")
    return pack


def verified_pack_payment(client, session, pack_id, customer_id):
    """Provider reads outside the user lock: paid session, the pack price once, a succeeded matching PaymentIntent."""
    price, cents = offers.option_id("stripe", "question_pack"), offers.STRIPE_CENTS["question_pack"]
    if price is None:
        raise HTTPException(503, "Pack price is not configured")
    if session.get("payment_status") != "paid":
        raise HTTPException(503, "Pack payment confirmation is pending")
    if session.get("amount_total") != cents or session.get("currency") != "usd":
        raise ValueError("Unexpected pack amount")
    items = client.checkout.sessions.line_items.list(session["id"])
    if isinstance(items, stripe.StripeObject): items = items.to_dict(recursive=True)
    if items.get("has_more") or len(items["data"]) != 1:
        raise ValueError("Unexpected pack lines")
    item = items["data"][0]
    if ((item.get("price") or {}).get("id") != price or item.get("quantity") != 1
            or item.get("amount_total") != cents or item.get("currency") != "usd"):
        raise ValueError("Unexpected pack line")
    intent = checked(client.payment_intents.retrieve(ident(session.get("payment_intent"), "pi")))
    if intent.get("status") != "succeeded":
        raise HTTPException(503, "Pack payment confirmation is pending")
    if (intent.get("currency") != "usd" or intent.get("amount_received") != cents
            or ident(intent.get("customer"), "cus") != customer_id or (intent.get("metadata") or {}).get("pack_id") != pack_id):
        raise ValueError("Unexpected pack payment")
    return intent["id"]


def process_pack_session(db, client, key, kind, occurred, session, account, live):
    """Exactly one grant per pack: state change under the user lock, in one transaction with the event row."""
    metadata = session.get("metadata") or {}
    pack = stripe_pack(db, metadata.get("pack_id"), account, live)
    if pack is None:
        return record_event(db, key, kind, occurred, "recorded_unmatched", None)
    pack_id, user_id, customer_id, saved_session = pack.id, pack.user_id, pack.customer_id, pack.session_id
    db.rollback()
    if not customer_id:
        raise HTTPException(503, "Customer binding is pending")
    if (metadata.get("user_id") != str(user_id) or ident(session["customer"], "cus") != customer_id
            or (saved_session and saved_session != session["id"])):
        raise ValueError("Pack session ownership mismatch")
    target, intent_id = None, None
    if kind in ("checkout.session.completed", "checkout.session.async_payment_succeeded"):
        target, intent_id = "paid", verified_pack_payment(client, session, pack_id, customer_id)
    elif kind == "checkout.session.async_payment_failed" and session.get("payment_status") != "paid":
        target = "failed"
    elif kind == "checkout.session.expired" and session.get("status") == "expired":
        target = "expired"
    locked_billing_user(db, user_id)
    insert = pg_insert if db.bind.dialect.name == "postgresql" else sqlite_insert
    result = db.execute(insert(BillingEvent).values(provider="stripe", event_id=key, event_type=kind,
        occurred_at=occurred, processed_at=datetime.utcnow(), outcome="processed", details={"pack_id": pack_id}
    ).on_conflict_do_nothing(index_elements=["event_id"]))
    if result.rowcount == 0: db.commit(); return "duplicate"
    pack = db.get(QuestionPack, pack_id); db.refresh(pack)
    outcome = "pack_unchanged"  # already paid, revoked or closed: another event about the same payment
    if target and pack.state in ("creating", "ready"):
        pack.state, pack.session_id = target, session["id"]
        if target == "paid":
            pack.paid_at, pack.payment_intent_id = datetime.utcnow(), intent_id
        outcome = "pack_granted" if target == "paid" else "pack_" + target
    db.get(BillingEvent, key).outcome = outcome
    db.commit()
    return outcome


def process_pack_financial(db, client, key, kind, occurred, financial, payment_intent, account, live):
    """Refund or dispute of a pack payment removes only its unused remainder. None: not a pack payment."""
    pack = db.query(QuestionPack).filter_by(provider="stripe", account_id=account, livemode=live,
                                            payment_intent_id=payment_intent).first()
    if pack is None:
        db.rollback()  # a refund may arrive before the grant: the PaymentIntent names its pack
        intent = checked(client.payment_intents.retrieve(payment_intent))
        pack = stripe_pack(db, (intent.get("metadata") or {}).get("pack_id"), account, live)
        if pack and (not pack.customer_id or ident(intent.get("customer"), "cus") != pack.customer_id
                     or pack.payment_intent_id not in (None, payment_intent)):
            raise ValueError("Pack payment mismatch")
    if pack is None:
        return None
    if kind == "charge.dispute.created":
        reason = "dispute"
    elif kind == "charge.refunded":
        reason = "refund" if financial.get("amount_refunded", 0) > 0 else None
    elif kind.startswith("refund.") and kind != "refund.failed" and financial.get("status") not in ("failed", "canceled"):
        reason = "refund"
    else:
        reason = None  # a closed dispute never restores the remainder by itself
    pack_id, user_id = pack.id, pack.user_id
    db.rollback()
    locked_billing_user(db, user_id)
    insert = pg_insert if db.bind.dialect.name == "postgresql" else sqlite_insert
    result = db.execute(insert(BillingEvent).values(provider="stripe", event_id=key, event_type=kind,
        occurred_at=occurred, processed_at=datetime.utcnow(), outcome="processed", details={**financial, "pack_id": pack_id}
    ).on_conflict_do_nothing(index_elements=["event_id"]))
    if result.rowcount == 0: db.commit(); return "duplicate"
    pack = db.get(QuestionPack, pack_id); db.refresh(pack)
    outcome = "pack_financial_recorded"
    if reason and pack.state != "revoked":
        pack.state, pack.revoked_at, pack.revoke_reason = "revoked", datetime.utcnow(), reason
        pack.payment_intent_id = pack.payment_intent_id or payment_intent
        outcome = "pack_revoked"
    db.get(BillingEvent, key).outcome = outcome
    db.commit()
    return outcome


def record_event(db, key, kind, occurred, outcome, details):
    insert = pg_insert if db.bind.dialect.name == "postgresql" else sqlite_insert
    result = db.execute(insert(BillingEvent).values(provider="stripe", event_id=key, event_type=kind,
        occurred_at=occurred, processed_at=datetime.utcnow(), outcome=outcome, details=details
    ).on_conflict_do_nothing(index_elements=["event_id"]))
    db.commit()
    return outcome if result.rowcount else "duplicate"
