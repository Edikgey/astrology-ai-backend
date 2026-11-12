"""Fake Lava credentials/responses only. Never calls a real payment service."""
import copy
import json
import logging
import os
import threading
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta
from unittest.mock import patch, Mock
from uuid import uuid4

import httpx
from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError, OperationalError
import test_my_charts as fixtures
from database.queries import User, LavaCheckout, StripeCheckout, BillingEvent, GPTUsage
from modules import lava_billing as billing, lava_client as api, usage

OFFER, PRODUCT, CONTRACT, RENEWAL = [str(uuid4()) for _ in range(4)]
SECRET = "isolated-lava-webhook-secret-for-tests"
ENV = {"LAVA_API_KEY": "fake-test-key", "LAVA_WEBHOOK_SECRET": SECRET, "LAVA_OFFER_ID": OFFER,
       "LAVA_SUCCESS_RETURN_URL": "https://example.test/my-charts?billing=lava",
       "LAVA_FAILURE_RETURN_URL": "https://example.test/my-charts?billing=lava",
       "LAVA_CANCEL_RETURN_URL": "https://example.test/my-charts?billing=lava"}


def iso(value):
    return value.isoformat() + "Z"


# The real functions, captured before any test patches them (PAY-LAVA-STUCK regression).
REAL_LAVA_REQUEST = api.request
REAL_HTTPX_CLIENT = httpx.Client


def catalog():
    return {"items": [{"id": PRODUCT, "type": "SUBSCRIPTION", "offers": [{"id": OFFER,
        "prices": [{"currency": "RUB", "amount": 799, "periodicity": "MONTHLY"}]}]}]}


class InvoiceScript:
    """Synthetic Lava behind httpx.MockTransport for the real lava_client.request.

    The first invoice POST gets `first()` (an httpx.Response, or an httpx exception that is raised);
    every later POST gets a fresh valid 201 invoice. The catalog is served; anything else is 404.
    `before_first` runs inside the first POST before it answers (a race test holds tab A there).
    `posts` counts invoice POSTs that reached the transport: the proof that nothing is retried."""

    def __init__(self, first, before_first=None, products=None):
        self.first, self.before_first, self.catalog = first, before_first, products or catalog()
        self.lock, self.posts = threading.Lock(), 0

    def __call__(self, request):
        if request.method == "POST" and request.url.path == "/api/v3/invoice":
            with self.lock:
                self.posts += 1
                number = self.posts
            if number == 1 and self.first is not None:
                if self.before_first:
                    self.before_first()
                outcome = self.first()
                if isinstance(outcome, Exception):
                    raise outcome
                return outcome
            return httpx.Response(201, json={"id": str(uuid4()), "paymentUrl": f"https://pay.example.test/invoice/{number}",
                                             "amountTotal": {"amount": 799, "currency": "RUB"}})
        if request.method == "GET" and request.url.path == "/api/v2/products":
            return httpx.Response(200, json=self.catalog)
        return httpx.Response(404, json={})

    def installed(self):
        # A new client per call, as in production: `with httpx.Client(...)` closes it.
        return patch("modules.lava_client.httpx.Client",
                     side_effect=lambda **kwargs: REAL_HTTPX_CLIENT(transport=httpx.MockTransport(self)))


class LavaTests(unittest.TestCase):
    tearDown = fixtures.MyChartsTests.tearDown

    def setUp(self):
        fixtures.MyChartsTests.setUp(self)
        env = patch.dict(os.environ, ENV); env.start(); self.addCleanup(env.stop)
        self.start = datetime.utcnow() - timedelta(days=2)
        self.end = self.start + timedelta(days=31)
        self.snapshot = {"id": CONTRACT, "datetime": iso(self.start), "status": "COMPLETED",
            "receipt": {"amount": 799, "currency": "RUB"}, "buyer": {"email": "a@example.test"},
            "periodicity": "MONTHLY", "subscriptionStatus": "ACTIVE", "expiredAt": iso(self.end),
            "cancelledAt": None, "terminatedAt": None, "recurrentPayments": []}
        self.catalog = {"items": [{"id": PRODUCT, "type": "SUBSCRIPTION", "offers": [{"id": OFFER,
            "prices": [{"currency": "RUB", "amount": 799, "periodicity": "MONTHLY"}]}]}]}
        self.invoice = {"id": str(uuid4()), "status": "NEW", "amountTotal": {"amount": 799, "currency": "RUB"},
                        "paymentUrl": "https://pay.example.test/invoice"}
        self.network = patch("modules.lava_client.request", side_effect=self.provider).start()
        self.addCleanup(patch.stopall)
        with self.sessions() as db:
            db.add(LavaCheckout(id="bound", user_id=1, contract_id=CONTRACT, offer_id=OFFER,
                product_id=PRODUCT, buyer_email="a@example.test", state="ready", created_at=self.start - timedelta(seconds=10)))
            db.commit()

    def provider(self, method, path, **kwargs):
        if method == "GET" and path.startswith("/api/v2/products"):
            return copy.deepcopy(self.catalog)
        if method == "POST": return copy.deepcopy(self.invoice)
        if method == "DELETE": return None
        if path.startswith("/api/v2/invoices/"):
            return {"id": path.rsplit("/", 1)[1], "buyer": {"email": "a@example.test" if path.endswith(CONTRACT) else "b@example.test"},
                    "status": "FAILED" if self.snapshot["status"] == "FAILED" else "NEW",
                    "parentInvoice": {"id": CONTRACT}}
        return copy.deepcopy(self.snapshot)

    def event(self, kind="payment.success", at=None):
        value = {"eventType": kind, "contractId": CONTRACT, "timestamp": iso(at or self.start),
                 "buyer": {"email": "a@example.test"}, "product": {"id": PRODUCT},
                 "amount": 799, "currency": "RUB", "status": "subscription-active"}
        if kind.endswith("failed"): value["status"] = "subscription-failed"
        if "recurring" in kind:
            value.update(contractId=RENEWAL, parentContractId=CONTRACT)
        if kind == "subscription.cancelled":
            value.update(cancelledAt=iso(at or datetime.utcnow()), willExpireAt=iso(self.end))
        return value

    def financial(self, kind="refund.success", canceled=False, partial=False):
        data = {"amount": 100 if partial else 799, "currency": "RUB", "subscription_cancelled": canceled,
                "customer_email": "a@example.test", "product": {"product_id": PRODUCT, "tier_id": OFFER},
                "balance_impact": {"debited_amount": 100}}
        if kind == "refund.success": data.update(refund_id=str(uuid4()), refund_type="partial" if partial else "full")
        else: data.update(chargeback_id=str(uuid4()), dispute_date="2026-09-29")
        return {"event_id": str(uuid4()), "event_type": kind, "created_at": iso(datetime.utcnow()), "data": data}

    def send(self, event, secret=SECRET):
        return self.client.post("/payments/lava/webhook", json=event, headers={"X-Api-Key": secret})

    def state(self):
        r = self.client.get("/account/usage", headers=self.owner)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def test_checkout_auth_server_values_and_idempotent_reuse(self):
        for headers in ({}, self.guest):
            self.assertEqual(self.client.post("/payments/lava/checkout", headers=headers).status_code, 401)
        r = self.client.post("/payments/lava/checkout", headers=self.other, json={"offerId": "wrong", "amount": 1, "user_id": 1})
        self.assertEqual(r.status_code, 200, r.text)
        posted = [c for c in self.network.call_args_list if c.args[0] == "POST"]
        self.assertEqual(len(posted), 1)
        body = posted[0].kwargs["json"]
        self.assertEqual((body["offerId"], body["email"], body["currency"], body["periodicity"]), (OFFER, "b@example.test", "RUB", "MONTHLY"))
        self.assertNotIn("amount", body)
        self.assertEqual(self.client.post("/payments/lava/checkout", headers=self.other).json(), r.json())
        self.assertEqual(len([c for c in self.network.call_args_list if c.args[0] == "POST"]), 1)

    def test_definitive_email_rejection_does_not_leave_attempt_in_progress(self):
        provider=self.provider
        def rejected(method,path,**kwargs):
            if method=='POST':raise api.InvoiceEmailRejected()
            return provider(method,path,**kwargs)
        self.network.side_effect=rejected
        for _ in range(2):
            r=self.client.post('/payments/lava/checkout',headers=self.other)
            self.assertEqual(r.status_code,422)
            self.assertIn('Lava отклонила email',r.json()['detail'])
        with self.sessions() as db:
            attempts=db.query(LavaCheckout).filter_by(user_id=2).all()
            self.assertEqual(len(attempts),2)
            self.assertTrue(all(c.state=='rejected' and not c.contract_id and not c.payment_url for c in attempts))
            self.assertEqual(db.get(User,2).plan,'free')
        self.network.side_effect=provider
        r=self.client.post('/payments/lava/checkout',headers=self.other)
        self.assertEqual(r.status_code,200)
        self.assertEqual(self.client.post('/payments/lava/checkout',headers=self.other).json(),r.json())

    def test_missing_config_and_wrong_catalog_price_fail_before_invoice(self):
        with patch.dict(os.environ, {"LAVA_API_KEY": ""}):
            self.assertEqual(self.client.post("/payments/lava/checkout", headers=self.other).status_code, 503)
        self.catalog["items"][0]["offers"][0]["prices"][0]["amount"] = 66917.45
        self.assertEqual(self.client.post("/payments/lava/checkout", headers=self.other).status_code, 503)
        self.assertFalse(any(c.args[0] == "POST" for c in self.network.call_args_list))

    def test_malformed_invoice_keeps_ambiguous_checkout_blocked(self):
        self.invoice["paymentUrl"] = "javascript:bad"
        self.assertEqual(self.client.post("/payments/lava/checkout", headers=self.other).status_code, 502)
        self.assertEqual(self.client.post("/payments/lava/checkout", headers=self.other).status_code, 409)
        self.assertEqual(self.state()["plan"], "free")

    def test_api_checkout_error_keeps_ambiguous_attempt(self):
        def fail(method, path, **kwargs):
            if method == "POST": raise HTTPException(502, "provider unavailable")
            return self.provider(method, path, **kwargs)
        self.network.side_effect = fail
        self.assertEqual(self.client.post("/payments/lava/checkout", headers=self.other).status_code, 502)
        self.assertEqual(self.client.post("/payments/lava/checkout", headers=self.other).status_code, 409)

    def test_authentication_unknown_and_malformed_events(self):
        for secret in ("", "wrong"):
            self.assertEqual(self.send(self.event(), secret).status_code, 401)
        self.assertEqual(self.send({"eventType": "future.event"}).status_code, 200)
        self.assertEqual(self.send({"eventType": "payment.success"}).status_code, 400)
        financial = self.financial(); financial["data"]["subscription_cancelled"] = "false"
        self.assertEqual(self.send(financial).status_code, 400)
        self.assertEqual(self.state()["plan"], "free")

    def test_first_payment_and_duplicate_preserve_usage(self):
        value = self.event()
        r = self.send(value); self.assertEqual(r.status_code, 200, r.text)
        with self.sessions() as db:
            u = db.get(User, 1)
            self.assertEqual((u.payment_provider, u.provider_subscription_id), ("lava", CONTRACT))
            self.assertIsNone(u.provider_customer_id)
            db.add(GPTUsage(user_id=1, chart_id=1000, status="succeeded", plan="premium",
                            period_start=self.start, period_end=self.end)); db.commit()
        self.network.reset_mock()
        self.assertEqual(self.send(value).json()["outcome"], "duplicate")
        self.network.assert_not_called()
        state = self.state()
        self.assertEqual((state["plan"], state["gpt_messages_used"]), ("premium", 1))
        self.assertFalse(state["can_manage_subscription"])
        self.assertTrue(state["can_cancel_subscription"])

    def test_failed_initial_payment_never_grants(self):
        self.snapshot.update(status="FAILED", subscriptionStatus="FAILED", expiredAt=None)
        r = self.send(self.event("payment.failed")); self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.state()["plan"], "free")
        with self.sessions() as db:
            self.assertIsNone(db.get(User, 1).payment_provider)

    def test_pending_reopen_reuses_url_without_invoice(self):
        with self.sessions() as db:
            db.get(LavaCheckout, "bound").payment_url = "https://pay.example.test/existing"
            db.commit()
        for _ in range(2):
            r = self.client.post("/payments/lava/checkout", headers=self.owner)
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(r.json()["url"], "https://pay.example.test/existing")
        self.assertFalse(any(c.args[0] == "POST" for c in self.network.call_args_list))

    def test_failed_checkout_allows_retry_and_stripe_switch(self):
        from modules import stripe_billing
        from test_stripe import ENV as STRIPE_ENV
        self.snapshot.update(status="FAILED", subscriptionStatus="FAILED", expiredAt=None)
        self.assertEqual(self.send(self.event("payment.failed")).status_code, 200)
        with patch.dict(os.environ, STRIPE_ENV), patch.object(stripe_billing, "create_checkout", wraps=stripe_billing.create_checkout), patch.object(stripe_billing, "api") as client:
            from test_stripe import price, CUSTOMER
            import time
            client.return_value.prices.retrieve.return_value = price()
            client.return_value.customers.create.return_value = {"id": CUSTOMER, "livemode": True}
            client.return_value.checkout.sessions.create.return_value = {"id": "cs_live_test", "livemode": True, "status": "open", "client_secret": "cs_live_test_secret_test", "expires_at": int(time.time()) + 3600}
            r = self.client.post("/payments/stripe/checkout", headers=self.owner)
            self.assertEqual(r.status_code, 200, r.text)
            client.return_value.checkout.sessions.create.assert_called_once()
        with self.sessions() as db:
            self.assertEqual(db.get(LavaCheckout, "bound").state, "failed")
        late = self.event("payment.failed", datetime.utcnow())
        self.assertEqual(self.send(late).json()["outcome"], "processed")
        with self.sessions() as db:
            self.assertEqual(db.get(LavaCheckout, "bound").state, "failed")

    def test_unconfirmed_failure_does_not_release_pending_checkout(self):
        self.snapshot.update(status="IN_PROGRESS", subscriptionStatus="ACTIVE")
        self.assertEqual(self.send(self.event("payment.failed")).status_code, 503)
        with self.sessions() as db:
            self.assertEqual(db.get(LavaCheckout, "bound").state, "ready")

    def test_confirmed_failed_pending_is_reconciled_before_retry(self):
        self.snapshot.update(status="FAILED", subscriptionStatus="FAILED", expiredAt=None)
        r = self.client.post("/payments/lava/checkout", headers=self.owner)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(len([c for c in self.network.call_args_list if c.args[0] == "POST"]), 1)

    def test_failed_webhook_then_retry(self):
        self.snapshot.update(status="FAILED", subscriptionStatus="FAILED", expiredAt=None)
        self.assertEqual(self.send(self.event("payment.failed")).status_code, 200)
        self.assertEqual(self.client.post("/payments/lava/checkout", headers=self.owner).status_code, 200)
        self.assertEqual(len([c for c in self.network.call_args_list if c.args[0] == "POST"]), 1)

    def test_legacy_failed_billing_owner_is_released_on_verified_switch(self):
        from modules import stripe_billing
        from test_stripe import ENV as STRIPE_ENV
        self.snapshot.update(status="FAILED", subscriptionStatus="FAILED", expiredAt=None)
        with self.sessions() as db:
            u = db.get(User, 1)
            u.payment_provider, u.provider_subscription_id, u.subscription_status = "lava", CONTRACT, "failed"
            db.get(LavaCheckout, "bound").state = "failed"
            db.commit()
        with patch.dict(os.environ, STRIPE_ENV), patch.object(stripe_billing, "create_checkout", wraps=stripe_billing.create_checkout), patch.object(stripe_billing, "api") as client:
            from test_stripe import price, CUSTOMER
            import time
            client.return_value.prices.retrieve.return_value = price()
            client.return_value.customers.create.return_value = {"id": CUSTOMER, "livemode": True}
            client.return_value.checkout.sessions.create.return_value = {"id": "cs_live_test", "livemode": True, "status": "open", "client_secret": "cs_live_test_secret_test", "expires_at": int(time.time()) + 3600}
            r = self.client.post("/payments/stripe/checkout", headers=self.owner)
            self.assertEqual(r.status_code, 200, r.text)

    def test_reconciliation_failure_keeps_intent_and_never_creates_invoice(self):
        self.network.side_effect = HTTPException(502, "unavailable")
        self.assertEqual(self.client.post("/payments/lava/checkout", headers=self.owner).status_code, 502)
        with self.sessions() as db:
            self.assertEqual(db.get(LavaCheckout, "bound").state, "ready")
            self.assertEqual(db.query(LavaCheckout).count(), 1)
        self.assertFalse(any(c.args[0] == "POST" for c in self.network.call_args_list))

    def test_pending_lava_allows_stripe_but_paid_lava_blocks_new_checkout(self):
        from modules import stripe_billing
        from test_stripe import ENV as STRIPE_ENV
        with patch.dict(os.environ, STRIPE_ENV), patch.object(stripe_billing, "create_checkout", wraps=stripe_billing.create_checkout), patch.object(stripe_billing, "api") as client:
            from test_stripe import price, CUSTOMER
            import time
            client.return_value.prices.retrieve.return_value = price()
            client.return_value.customers.create.return_value = {"id": CUSTOMER, "livemode": True}
            client.return_value.checkout.sessions.create.return_value = {"id": "cs_live_test", "livemode": True, "status": "open", "client_secret": "cs_live_test_secret_test", "expires_at": int(time.time()) + 3600}
            self.assertEqual(self.client.post("/payments/stripe/checkout", headers=self.owner).status_code, 200)
            self.send(self.event())
            self.assertEqual(self.client.post("/payments/stripe/checkout", headers=self.owner).status_code, 409)
            self.assertEqual(self.client.post("/payments/lava/checkout", headers=self.owner).status_code, 409)
            client.return_value.checkout.sessions.create.assert_called_once()

    def test_renewal_uses_exact_api_period_duplicate_and_old_failure_safe(self):
        self.send(self.event())
        self.snapshot["recurrentPayments"] = [{"id": RENEWAL, "datetime": iso(self.end), "status": "COMPLETED", "amount": 799, "currency": "RUB"}]
        self.snapshot["expiredAt"] = iso(self.end + timedelta(days=28))
        value = self.event("subscription.recurring.payment.success", self.end)
        self.assertEqual(self.send(value).status_code, 200)
        self.assertEqual(self.send(value).json()["outcome"], "duplicate")
        self.assertEqual(self.send(self.event("payment.failed")).json()["outcome"], "ignored_stale")
        with self.sessions() as db:
            user = db.get(User, 1)
            self.assertEqual((user.current_period_start, user.current_period_end), (self.end, self.end + timedelta(days=28)))

    def test_failed_renewal_preserves_paid_period(self):
        self.send(self.event())
        self.snapshot["subscriptionStatus"] = "FAILED"
        self.assertEqual(self.send(self.event("subscription.recurring.payment.failed", datetime.utcnow())).status_code, 200)
        with self.sessions() as db:
            user = db.get(User, 1)
            self.assertEqual((user.plan, user.subscription_status, user.current_period_end), ("premium", "past_due", self.end))

    def test_cancellation_schedules_confirmed_expiry_and_duplicate_safe(self):
        self.send(self.event())
        self.snapshot.update(subscriptionStatus="CANCELLED", cancelledAt=iso(datetime.utcnow()))
        value = self.event("subscription.cancelled")
        self.assertEqual(self.send(value).status_code, 200)
        self.assertEqual(self.send(value).json()["outcome"], "duplicate")
        self.assertEqual(self.state()["plan"], "premium")
        with self.sessions() as db: self.assertEqual(db.get(User, 1).scheduled_cancel_at, self.end)

    def test_terminated_subscription_revokes(self):
        self.send(self.event())
        self.snapshot.update(subscriptionStatus="CANCELLED", terminatedAt=iso(datetime.utcnow()))
        self.assertEqual(self.send(self.event("subscription.cancelled")).status_code, 200)
        self.assertEqual(self.state()["plan"], "free")

    def test_full_and_partial_refund_and_chargeback_without_cancel_retain_access(self):
        self.send(self.event())
        for kind, partial in (("refund.success", False), ("refund.success", True), ("chargeback.initiated", False)):
            value = self.financial(kind, partial=partial)
            self.assertEqual(self.send(value).status_code, 200)
            self.assertEqual(self.send(value).json()["outcome"], "duplicate")
            self.assertEqual(self.state()["plan"], "premium")
            with self.sessions() as db:
                self.assertFalse(db.get(BillingEvent, "lava:" + value["event_id"]).details["subscription_cancelled"])

    def test_refund_and_chargeback_cancel_only_after_contract_state_verified(self):
        self.send(self.event())
        for kind in ("refund.success", "chargeback.initiated"):
            value = self.financial(kind, canceled=True)
            self.assertEqual(self.send(value).status_code, 503)
            self.snapshot.update(subscriptionStatus="CANCELLED", cancelledAt=iso(datetime.utcnow()))
            r = self.send(value); self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(self.send(value).json()["outcome"], "duplicate")
            self.assertTrue(self.state()["cancel_at_period_end"])
            self.snapshot.update(subscriptionStatus="ACTIVE", cancelledAt=None)

    def test_ambiguous_refund_email_does_not_revoke(self):
        self.send(self.event())
        with self.sessions() as db:
            db.add(LavaCheckout(user_id=1, contract_id=str(uuid4()), offer_id=OFFER, product_id=PRODUCT,
                buyer_email="a@example.test", state="completed", created_at=self.start)); db.commit()
        self.assertEqual(self.send(self.financial(canceled=True)).json()["outcome"], "recorded_unmatched")
        self.assertEqual(self.state()["plan"], "premium")

    def test_provider_owned_ids_and_checkout_isolation(self):
        with self.sessions() as db:
            user = db.get(User, 1)
            user.payment_provider = "stripe"; user.provider_subscription_id = CONTRACT
            usage.apply_plan(db, user, "premium", self.start, self.end); db.commit()
        self.assertEqual(self.send(self.event()).json()["outcome"], "billing_conflict")
        self.assertEqual(self.client.post("/payments/lava/checkout", headers=self.owner).status_code, 409)
        self.assertEqual(self.client.post("/payments/lava/cancel", headers=self.owner).status_code, 409)
        with self.sessions() as db: self.assertEqual(db.get(User, 1).payment_provider, "stripe")

    def test_unique_ids_scoped_and_same_provider_duplicate_denied(self):
        with self.sessions() as db:
            for uid, provider in ((1, "stripe"), (2, "lava")):
                user = db.get(User, uid); user.payment_provider = provider
                user.provider_customer_id = "same"; user.provider_subscription_id = CONTRACT
            db.commit()
            db.get(User, 2).payment_provider = "stripe"
            with self.assertRaises(IntegrityError): db.commit()
            db.rollback()
            for provider in ("stripe", "lava"):
                db.add(BillingEvent(provider=provider, event_id="same" if provider == "stripe" else "lava:same", event_type="test", occurred_at=self.start))
            db.commit()

    def test_cancel_uses_bound_contract_and_email_once_waits_for_webhook(self):
        self.send(self.event())
        self.assertEqual(self.client.post("/payments/lava/cancel", headers=self.other).status_code, 409)
        for _ in range(2):
            r = self.client.post("/payments/lava/cancel", headers=self.owner, json={"contractId": "foreign"})
            self.assertEqual(r.status_code, 200, r.text)
        calls = [c for c in self.network.call_args_list if c.args[0] == "DELETE"]
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].kwargs["params"], {"contractId": CONTRACT, "email": "a@example.test"})
        self.assertEqual(self.state()["plan"], "premium")

    def test_provider_binding_mismatch_api_failure_and_bad_expiry_never_grant(self):
        self.snapshot["buyer"]["email"] = "different@example.test"
        self.assertEqual(self.send(self.event()).status_code, 400)
        self.snapshot["buyer"]["email"] = "a@example.test"
        self.snapshot["expiredAt"] = None
        self.assertEqual(self.send(self.event()).status_code, 400)
        self.network.side_effect = HTTPException(502, "unavailable")
        self.assertEqual(self.send(self.event()).status_code, 502)
        self.assertEqual(self.state()["plan"], "free")
        with self.sessions() as db: self.assertEqual(db.query(BillingEvent).count(), 0)

    def test_api_eventual_consistency_does_not_acknowledge_unconfirmed_payment(self):
        self.snapshot["status"] = "IN_PROGRESS"
        value = self.event()
        self.assertEqual(self.send(value).status_code, 503)
        self.snapshot["status"] = "COMPLETED"
        self.assertEqual(self.send(value).status_code, 200)
        self.assertEqual(self.state()["plan"], "premium")

    def test_stripe_pending_checkout_allows_lava(self):
        with self.sessions() as db:
            db.add(StripeCheckout(user_id=2, state="creating", account_id="acct_test", livemode=True)); db.commit()
        self.assertEqual(self.client.post("/payments/lava/checkout", headers=self.other).status_code, 200)
        self.assertTrue(any(c.args[0] == "POST" for c in self.network.call_args_list))

    def test_terminal_cancel_cannot_be_undone_by_same_payment_active_snapshot(self):
        self.send(self.event())
        self.snapshot.update(subscriptionStatus="CANCELLED", terminatedAt=iso(datetime.utcnow()))
        self.send(self.event("subscription.cancelled"))
        self.snapshot.update(subscriptionStatus="ACTIVE", terminatedAt=None)
        response = self.send(self.event(at=datetime.utcnow() + timedelta(seconds=1)))
        self.assertEqual(response.json()["outcome"], "ignored_stale_snapshot")
        self.assertEqual(self.state()["plan"], "free")

    def test_same_provider_resubscribe_and_late_old_contract_is_ignored(self):
        self.send(self.event())
        self.snapshot.update(subscriptionStatus="CANCELLED", terminatedAt=iso(datetime.utcnow()))
        self.send(self.event("subscription.cancelled"))
        new_id = str(uuid4())
        with self.sessions() as db:
            db.add(LavaCheckout(user_id=1, contract_id=new_id, offer_id=OFFER, product_id=PRODUCT,
                buyer_email="a@example.test", state="ready")); db.commit()
        self.snapshot.update(id=new_id, subscriptionStatus="ACTIVE", terminatedAt=None, cancelledAt=None)
        value = self.event(at=datetime.utcnow()); value["contractId"] = new_id
        self.assertEqual(self.send(value).status_code, 200)
        self.assertEqual(self.send(self.event("subscription.cancelled")).json()["outcome"], "ignored_other_subscription")
        self.assertEqual(self.state()["plan"], "premium")


class LavaHttpTests(unittest.TestCase):
    def test_only_exact_invoice_email_rejection_is_safe_to_release(self):
        original=httpx.Client
        cases=[(400,{'error':'Incorrect email to purchase'},True),
               (400,{'error':'Other validation failure'},False),
               (400,{'error':'Incorrect email to purchase','id':CONTRACT},False),
               (500,{'error':'Incorrect email to purchase'},False)]
        for status,body,rejected in cases:
            with self.subTest(status=status,body=body),patch.dict(os.environ,ENV):
                handler=Mock(return_value=httpx.Response(status,json={**body,'details':'sensitive-provider-data'}))
                client=original(transport=httpx.MockTransport(handler))
                with patch('modules.lava_client.httpx.Client',return_value=client), self.assertLogs('modules.lava_client',level='WARNING') as logs:
                    with self.assertRaises(HTTPException) as caught:api.request('POST','/api/v3/invoice',json={})
                self.assertEqual(isinstance(caught.exception,api.InvoiceEmailRejected),rejected)
                self.assertEqual(caught.exception.status_code,422 if rejected else 502)
                self.assertEqual(handler.call_count,1)
                self.assertNotIn('sensitive-provider-data',str(logs.output)+str(caught.exception.detail))

    def test_failure_diagnostics_exclude_provider_data_and_do_not_retry(self):
        import json
        original = httpx.Client
        secret = "sensitive-key-email-signature-body"
        cases = [
            (httpx.ReadTimeout(secret), "request", "ReadTimeout", None),
            (httpx.Response(200, content=secret.encode()), "decode_response", "JSONDecodeError", 200),
            (httpx.Response(500, content=secret.encode()), "http_status", "HTTPException", 500),
            # PAY-LAVA-STUCK T7: a definitive refusal is labelled so support sees the attempt was closed.
            (httpx.Response(429, content=secret.encode()), "http_status", "InvoiceNotCreated", 429),
            (httpx.Response(403, content=secret.encode()), "http_status", "InvoiceNotCreated", 403),
            (httpx.ConnectError(secret), "request", "ConnectError", None),
        ]
        for response, stage, exception, status in cases:
            with self.subTest(status=status, stage=stage, exception=exception), patch.dict(os.environ, {**ENV, "LAVA_API_KEY": secret}):
                handler = Mock(side_effect=response) if isinstance(response, Exception) else Mock(return_value=response)
                client = original(transport=httpx.MockTransport(handler))
                with patch("modules.lava_client.httpx.Client", return_value=client), self.assertLogs("modules.lava_client", level="WARNING") as logs:
                    with self.assertRaises(HTTPException) as caught:
                        api.request("POST", "/api/v3/invoice", json={"email": secret})
                self.assertEqual(caught.exception.status_code, 503 if status == 429 else 502)
                self.assertEqual(handler.call_count, 1)
                self.assertEqual(len(logs.records), 1)
                record = logs.records[0]
                data = json.loads(record.getMessage().split("Lava request failed ", 1)[1])
                self.assertEqual(set(data), {"operation", "stage", "exception_class", "duration_ms", "http_status"})
                self.assertEqual((data["operation"], data["stage"], data["exception_class"], data["http_status"]),
                                 ("invoice_create", stage, exception, status))
                self.assertGreaterEqual(data["duration_ms"], 0)
                self.assertIsNone(record.exc_info)
                self.assertNotIn(secret, str(logs.output) + caught.exception.detail)

    def test_invoice_timeout_is_extended_without_changing_other_operations(self):
        original = httpx.Client
        for method, path, read, connect in (("POST", "/api/v3/invoice", 30, 10),
                                           ("GET", "/api/v2/products", 4, 2),
                                           ("DELETE", "/api/v1/subscriptions", 4, 2)):
            with self.subTest(method=method), patch.dict(os.environ, ENV):
                handler = Mock(return_value=httpx.Response(200, json={}))
                client = original(transport=httpx.MockTransport(handler))
                with patch("modules.lava_client.httpx.Client", return_value=client) as factory:
                    api.request(method, path)
                timeout = factory.call_args.kwargs["timeout"]
                self.assertEqual((timeout.read, timeout.connect), (read, connect))
                self.assertFalse(factory.call_args.kwargs["follow_redirects"])
                self.assertEqual(handler.call_count, 1)

    def test_new_domain_returns_pass_through_configuration(self):
        target = "https://mylunariaai.com/my-charts?billing=lava"
        values = {**ENV, **{name: target for name in (
            "LAVA_SUCCESS_RETURN_URL", "LAVA_FAILURE_RETURN_URL", "LAVA_CANCEL_RETURN_URL")}}
        with patch.dict(os.environ, values):
            offer, returns = api.config()
        self.assertEqual(offer, OFFER)
        self.assertEqual(returns, {name: target for name in (
            "successful_return_url", "failure_return_url", "cancel_return_url")})

    def test_http_201_invoice_response_is_accepted(self):
        original = httpx.Client
        with patch.dict(os.environ, ENV):
            response = {"id": CONTRACT, "paymentUrl": "https://pay.example.test/", "amountTotal": {"amount": 799, "currency": "RUB"}}
            handler = Mock(return_value=httpx.Response(201, json=response))
            client = original(transport=httpx.MockTransport(handler))
            with patch("modules.lava_client.httpx.Client", return_value=client):
                self.assertEqual(api.request("POST", "/api/v3/invoice", json={}), response)
            self.assertEqual(handler.call_count, 1)

    def test_timeout_rate_limit_non_json_non_success_and_no_retry(self):
        original = httpx.Client
        for status, content in ((429, b'{}'), (500, b'{}'), (200, b'bad'), (302, b'')):
            with self.subTest(status=status), patch.dict(os.environ, ENV):
                handler = Mock(return_value=httpx.Response(status, content=content))
                client = original(transport=httpx.MockTransport(handler))
                with patch("modules.lava_client.httpx.Client", return_value=client):
                    with self.assertRaises(HTTPException): api.request("POST", "/api/v3/invoice", json={})
                self.assertEqual(handler.call_count, 1)
        with patch.dict(os.environ, ENV), patch("modules.lava_client.httpx.Client") as client:
            client.return_value.__enter__.return_value.request.side_effect = httpx.TimeoutException("unsafe provider text")
            with self.assertRaises(HTTPException) as caught: api.request("POST", "/api/v3/invoice")
            self.assertNotIn("unsafe", caught.exception.detail)

    # --- PAY-LAVA-STUCK T6 (tasks/PAY-LAVA-STUCK/DESIGN.md §2-§3): classify where the status is known ---
    def provider_call(self, outcome, method="POST", path="/api/v3/invoice"):
        def answer(request):
            value = outcome()
            if isinstance(value, Exception):
                raise value
            return value
        handler = Mock(side_effect=answer)
        kwargs = {"json": {}} if method == "POST" else {}
        with patch.dict(os.environ, ENV), patch("modules.lava_client.httpx.Client",
                side_effect=lambda **_: REAL_HTTPX_CLIENT(transport=httpx.MockTransport(handler))), \
                self.assertLogs("modules.lava_client", level="WARNING"):
            with self.assertRaises(HTTPException) as caught:
                api.request(method, path, **kwargs)
        self.assertEqual(handler.call_count, 1)  # one HTTP request per call, never an automatic retry
        return caught.exception

    def test_t6_definitive_invoice_refusals_raise_invoice_not_created(self):
        not_created = getattr(api, "InvoiceNotCreated", None)
        for name, outcome, status in [("429 json", RATE_LIMIT, 503), *DEFINITIVE, *NOT_SENT]:
            with self.subTest(name):
                error = self.provider_call(outcome)
                self.assertTrue(not_created is not None and isinstance(error, not_created),
                                f"{name}: {type(error).__name__} is not lava_client.InvoiceNotCreated")
                # Code, text and header the user sees stay exactly as before.
                self.assertEqual((error.status_code, error.detail, error.headers),
                                 (status, api.PROVIDER_SILENT, {"Retry-After": "60"} if status == 503 else None))
        with self.subTest("400 incorrect email"):
            error = self.provider_call(lambda: httpx.Response(400, json={"error": "Incorrect email to purchase"}))
            self.assertIsInstance(error, api.InvoiceEmailRejected)
            self.assertEqual(error.status_code, 422)
            self.assertTrue(not_created is not None and issubclass(api.InvoiceEmailRejected, not_created),
                            "InvoiceEmailRejected must be a kind of InvoiceNotCreated")

    def test_t6_unclear_outcomes_and_other_operations_stay_plain_errors(self):
        # Guard (green on 1c6bb25): an invoice may exist, or the call is not an invoice creation.
        not_created = getattr(api, "InvoiceNotCreated", None)

        def plain(error):
            return not_created is None or not isinstance(error, not_created)
        for name, outcome, status in UNCLEAR + CONTRADICTORY:
            with self.subTest("invoice_create " + name):
                error = self.provider_call(outcome)
                self.assertTrue(plain(error), f"{name}: an unclear outcome must not release the attempt")
                self.assertEqual((error.status_code, error.detail), (status, api.PROVIDER_SILENT))
        for operation, method, path in (("invoice_read", "GET", "/api/v2/invoices/" + CONTRACT),
                                        ("catalog_read", "GET", "/api/v2/products?showAllSubscriptionPeriods=true"),
                                        ("subscription_cancel", "DELETE", "/api/v1/subscriptions")):
            for name, outcome, status in (("429", RATE_LIMIT, 503), ("403", json_refusal(403), 502), NOT_SENT[0]):
                with self.subTest(operation + " " + name):
                    error = self.provider_call(outcome, method, path)
                    self.assertTrue(plain(error), f"{operation} {name}: only invoice_create is classified")
                    self.assertEqual((error.status_code, error.detail), (status, api.PROVIDER_SILENT))


# --- PAY-LAVA-STUCK outcome tables (DESIGN §2). Factories: every use gets a fresh response or exception. ---
def json_refusal(status, **body):
    return lambda: httpx.Response(status, json={"error": "synthetic refusal", **body})


def invoice_201(**changes):
    body = {"id": str(uuid4()), "paymentUrl": "https://pay.example.test/orphan",
            "amountTotal": {"amount": 799, "currency": "RUB"}}
    body.update(changes)
    return httpx.Response(201, json={k: v for k, v in body.items() if v is not None})


def RATE_LIMIT():
    return httpx.Response(429, json={})


# Definitive refusal: a full 4xx answer without `id` and `paymentUrl` (third column: our API status).
DEFINITIVE = [("400 other validation", lambda: httpx.Response(400, json={"error": "Other validation failure"}), 502),
              *[(str(status), json_refusal(status), 502) for status in (401, 403, 404, 405, 406, 408, 409, 410, 422)],
              ("429 html", lambda: httpx.Response(429, html="<html><body>Too Many Requests</body></html>"), 503)]
# The connection was never established: Lava received nothing.
NOT_SENT = [("ConnectError", lambda: httpx.ConnectError("synthetic: connection refused"), 502),
            ("ConnectTimeout", lambda: httpx.ConnectTimeout("synthetic: connect timeout"), 502)]
# Unclear outcome raised inside lava_client.request: an invoice may exist.
UNCLEAR = [*[(str(status), (lambda status=status: httpx.Response(status, json={})), 502) for status in (500, 502, 503, 504)],
           ("302", lambda: httpx.Response(302, headers={"Location": "https://gate.lava.top/elsewhere"}), 502),
           ("200 not json", lambda: httpx.Response(200, content=b"not json"), 502),
           ("200 list", lambda: httpx.Response(200, json=[]), 502),
           ("ReadTimeout", lambda: httpx.ReadTimeout("synthetic: read timeout"), 502),
           ("WriteTimeout", lambda: httpx.WriteTimeout("synthetic: write timeout"), 502),
           ("ReadError", lambda: httpx.ReadError("synthetic: read error"), 502),
           ("WriteError", lambda: httpx.WriteError("synthetic: write error"), 502),
           ("RemoteProtocolError", lambda: httpx.RemoteProtocolError("synthetic: closed after send"), 502)]
# Unclear outcome detected by create_checkout after request() returned.
BODY_UNCLEAR = [("201 without paymentUrl", lambda: invoice_201(paymentUrl=None), 502),
                ("201 amount 1", lambda: invoice_201(amountTotal={"amount": 1, "currency": "RUB"}), 502),
                ("204", lambda: httpx.Response(204), 502)]
# A 4xx that names an invoice contradicts itself: it stays an unclear outcome.
CONTRADICTORY = [("400 with id", lambda: httpx.Response(400, json={"error": "Other validation failure", "id": str(uuid4())}), 502),
                 ("429 with paymentUrl", lambda: httpx.Response(429, json={"paymentUrl": "https://pay.example.test/orphan"}), 503),
                 ("403 with id", lambda: json_refusal(403, id=str(uuid4()))(), 502)]


class LavaInvoiceRefusalTests(unittest.TestCase):
    """PAY-LAVA-STUCK T1-T5 (tasks/PAY-LAVA-STUCK/DESIGN.md §5): the endpoint over the real
    lava_client.request and a synthetic Lava. User 2 pays: user 1 already has the `bound` attempt."""
    tearDown = fixtures.MyChartsTests.tearDown
    provider = LavaTests.provider

    def setUp(self):
        LavaTests.setUp(self)
        self.network.side_effect = REAL_LAVA_REQUEST

    def attempts(self):
        with self.sessions() as db:
            rows = db.query(LavaCheckout).filter_by(user_id=2).all()
            return sorted(((c.state, c.contract_id, c.payment_url) for c in rows), key=lambda row: row[0])

    def pay_twice(self, first):
        with self.sessions() as db:  # clean state for every case
            db.query(LavaCheckout).filter_by(user_id=2).delete()
            db.commit()
            self.assertEqual(db.get(User, 2).plan, "free")
        script = InvoiceScript(first, products=self.catalog)
        with script.installed():
            one = self.client.post("/payments/lava/checkout", headers=self.other)
            after_one = self.attempts()
            two = self.client.post("/payments/lava/checkout", headers=self.other)
        return script, one, after_one, two

    def assert_closed_then_paid(self, first, status):
        script, one, after_one, two = self.pay_twice(first)
        self.assertEqual(one.status_code, status, one.text)
        self.assertEqual(one.json()["detail"], api.PROVIDER_SILENT)
        self.assertEqual(one.headers.get("retry-after"), "60" if status == 503 else None)
        # Red on 1c6bb25: the attempt stays `creating` and the next payment gets 409 forever.
        self.assertEqual((after_one, two.status_code), ([("rejected", None, None)], 200), two.text)
        self.assertEqual(two.json()["url"], "https://pay.example.test/invoice/2")
        self.assertEqual(script.posts, 2)  # the second POST is the user's new click, not a retry
        self.assertEqual([row[0] for row in self.attempts()], ["ready", "rejected"])
        with self.sessions() as db:
            self.assertEqual(db.get(User, 2).plan, "free")  # Premium only after a verified webhook

    def assert_still_blocked(self, first, status):
        script, one, after_one, two = self.pay_twice(first)
        self.assertEqual(one.status_code, status, one.text)
        self.assertEqual((after_one, two.status_code), ([("creating", None, None)], 409), two.text)
        self.assertEqual(two.json()["detail"], billing.CHECKOUT_PENDING)
        self.assertEqual(script.posts, 1)

    def test_t1_rate_limit_closes_attempt_and_next_payment_opens(self):
        self.assert_closed_then_paid(RATE_LIMIT, 503)

    def test_t2_definitive_4xx_closes_attempt_and_next_payment_opens(self):
        for name, first, status in DEFINITIVE:
            with self.subTest(name):
                self.assert_closed_then_paid(first, status)

    def test_t3_connection_not_established_closes_attempt_and_next_payment_opens(self):
        for name, first, status in NOT_SENT:
            with self.subTest(name):
                self.assert_closed_then_paid(first, status)

    def test_t4_unclear_outcome_still_blocks_until_reconciled(self):
        for name, first, status in UNCLEAR + BODY_UNCLEAR:
            with self.subTest(name):
                self.assert_still_blocked(first, status)

    def test_t5_4xx_naming_an_invoice_still_blocks(self):
        for name, first, status in CONTRADICTORY:
            with self.subTest(name):
                self.assert_still_blocked(first, status)


# --- PAY-LAVA-CANCEL (tasks/PAY-LAVA-CANCEL/DESIGN.md §3, §5): cancellation must not hang after a failed DELETE ---
SUBSCRIPTION_READ = "/api/v1/subscriptions/" + CONTRACT
CLAIM_NOT_RELEASED = "Lava cancel claim not released "
FAILURE_KEYS = {"operation", "stage", "exception_class", "duration_ms", "http_status"}


class CancelScript:
    """Synthetic Lava for subscription cancellation behind httpx.MockTransport for the real lava_client.request.

    `GET /api/v1/subscriptions/{CONTRACT}` serves a copy of `subscription`, or `read()` while `read` is set
    (an httpx.Response, or an httpx exception that is raised). The first DELETE gets `first()`; with `applied`
    the subscription is marked cancelled before that answer (Lava did it, we just never heard). Every other
    DELETE gets 204 and cancels. Anything else is 404. `calls` lists requests in arrival order ("GET", "DELETE",
    or "METHOD path" for anything unexpected); `params` holds the query of every DELETE. `before_first` runs
    inside the first DELETE before it answers (a race test holds tab A there); `before_read` inside every GET."""

    installed = InvoiceScript.installed

    def __init__(self, subscription, first=None, applied=False, read=None, before_first=None, before_read=None):
        self.subscription = copy.deepcopy(subscription)
        self.first, self.applied, self.read = first, applied, read
        self.before_first, self.before_read = before_first, before_read
        self.lock, self.calls, self.params = threading.Lock(), [], []

    @property
    def deletes(self):
        return self.calls.count("DELETE")

    def cancelled_at_lava(self):
        with self.lock:
            self.subscription.update(subscriptionStatus="CANCELLED", cancelledAt=iso(datetime.utcnow()))

    @staticmethod
    def answer(outcome):
        value = outcome()
        if isinstance(value, Exception):
            raise value
        return value

    def __call__(self, request):
        method, path = request.method, request.url.path
        if method == "GET" and path == SUBSCRIPTION_READ:
            with self.lock:
                self.calls.append("GET")
            if self.before_read:
                self.before_read()
            if self.read is not None:
                return self.answer(self.read)
            with self.lock:
                return httpx.Response(200, json=copy.deepcopy(self.subscription))
        if method == "DELETE" and path == "/api/v1/subscriptions":
            with self.lock:
                self.calls.append("DELETE")
                self.params.append(dict(request.url.params))
                number = self.calls.count("DELETE")
            if number == 1 and self.first is not None:
                if self.before_first:
                    self.before_first()
                if self.applied:
                    self.cancelled_at_lava()
                return self.answer(self.first)
            self.cancelled_at_lava()
            return httpx.Response(204)
        with self.lock:
            self.calls.append(f"{method} {path}")
        return httpx.Response(404, json={})


@contextmanager
def captured_logs():
    """Every WARNING+ record that reaches the root logger; unlike assertLogs, an empty capture is allowed."""
    records = []
    handler = logging.Handler(logging.WARNING)
    handler.emit = records.append
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        yield records
    finally:
        root.removeHandler(handler)


class LavaCancelTests(unittest.TestCase):
    """PAY-LAVA-CANCEL C1-C7, C11: POST /payments/lava/cancel over the real lava_client.request and a synthetic
    Lava. User 1 subscribes through a verified webhook (fake provider), then the transport takes over."""
    tearDown = fixtures.MyChartsTests.tearDown
    provider, event, send = LavaTests.provider, LavaTests.event, LavaTests.send
    DELETE_PARAMS = {"contractId": CONTRACT, "email": "a@example.test"}
    PENDING = (200, {"pending": True})

    def setUp(self):
        LavaTests.setUp(self)
        r = self.send(self.event())
        self.assertEqual(r.status_code, 200, r.text)
        self.network.side_effect = REAL_LAVA_REQUEST

    def subscriber(self):
        """(claim, plan, subscription_status, scheduled_cancel_at): only a verified webhook may change the last three."""
        with self.sessions() as db:
            user = db.get(User, 1)
            return (db.get(LavaCheckout, "bound").cancel_requested, user.plan, user.subscription_status,
                    user.scheduled_cancel_at)

    def fresh(self, **script):
        """Clean state for every case: no claim, the same paid subscriber, a new synthetic Lava."""
        with self.sessions() as db:
            db.get(LavaCheckout, "bound").cancel_requested = False
            db.commit()
        self.assertEqual(self.subscriber(), (False, "premium", "active", None))
        return CancelScript(self.snapshot, **script)

    def cancel(self):
        return self.client.post("/payments/lava/cancel", headers=self.owner)

    def assert_refused(self, response, status):
        """A failed or unclear cancellation is never shown as a success: the old error, code and header."""
        self.assertEqual((response.status_code, response.json().get("detail"), response.headers.get("retry-after")),
                         (status, api.PROVIDER_SILENT, "60" if status == 503 else None), response.text)

    def assert_access_untouched(self, claim=None):
        claim_now, *access = self.subscriber()
        self.assertEqual(access, ["premium", "active", None])  # access and dates come only from a verified webhook
        if claim is not None:
            self.assertEqual(claim_now, claim)

    def click_twice(self, script):
        with script.installed():
            one = self.cancel()
            after_one = (self.subscriber()[0], list(script.calls))
            two = self.cancel()
        return one, after_one, two

    def assert_released_then_cancelled(self, first, status):
        script = self.fresh(first=first)
        one, (claim, calls), two = self.click_twice(script)
        self.assert_refused(one, status)
        self.assertEqual(calls.count("DELETE"), 1)  # one DELETE per click, never an automatic retry
        # Red on 61e4abc: the claim stays true, so the next click answers "pending" without Lava (calls == ["DELETE"]).
        self.assertEqual((claim, two.status_code, script.calls), (False, 200, ["GET", "DELETE", "GET", "DELETE"]),
                         two.text)
        self.assertEqual(two.json(), {"pending": True})
        self.assertEqual(script.params, [self.DELETE_PARAMS] * 2)  # contract and email from the database
        self.assert_access_untouched(claim=True)

    def test_c1_rate_limited_cancel_releases_claim_and_next_click_reaches_lava(self):
        self.assert_released_then_cancelled(RATE_LIMIT, 503)

    def test_c2_definitive_refusal_or_no_connection_releases_claim(self):
        for name, first, status in DEFINITIVE + NOT_SENT:
            with self.subTest(name):
                self.assert_released_then_cancelled(first, status)

    def test_c3_unclear_outcome_not_applied_next_click_reads_then_deletes(self):
        for name, first, status in UNCLEAR:
            with self.subTest(name):
                self.assert_released_then_cancelled(first, status)

    def test_c4_unclear_outcome_applied_by_lava_never_sends_second_delete(self):
        # Guard (green on 61e4abc): no second DELETE once Lava shows the cancellation; never "cancelled" on error.
        for name, first, status in UNCLEAR:
            with self.subTest(name):
                script = self.fresh(first=first, applied=True)
                one, _, two = self.click_twice(script)
                self.assert_refused(one, status)
                self.assertEqual(((two.status_code, two.json()), script.deletes), (self.PENDING, 1), two.text)
                self.assert_access_untouched()

    def test_c5_cancellation_already_visible_at_lava_sends_no_delete(self):
        for name, changes in (("CANCELLED", {"subscriptionStatus": "CANCELLED"}),
                              ("cancelledAt only", {"cancelledAt": iso(datetime.utcnow())}),
                              ("terminatedAt only", {"terminatedAt": iso(datetime.utcnow())})):
            with self.subTest(name):
                script = self.fresh()
                script.subscription.update(changes)
                with script.installed():
                    r = self.cancel()
                # Red on 61e4abc: there is no read, the DELETE goes out and the claim is set.
                self.assertEqual(((r.status_code, r.json()), script.calls, self.subscriber()[0]),
                                 (self.PENDING, ["GET"], False), r.text)
                self.assert_access_untouched()

    def test_c6_failed_read_sends_no_delete_and_next_click_cancels(self):
        foreign = {**self.snapshot, "id": str(uuid4())}
        without_id = {k: v for k, v in self.snapshot.items() if k != "id"}
        for name, read, status in (("500", lambda: httpx.Response(500, json={}), 502),
                                   ("429", RATE_LIMIT, 503),
                                   ("ReadTimeout", lambda: httpx.ReadTimeout("synthetic: read timeout"), 502),
                                   ("200 foreign id", lambda: httpx.Response(200, json=foreign), 502),
                                   ("200 without id", lambda: httpx.Response(200, json=without_id), 502),
                                   ("204", lambda: httpx.Response(204), 502)):
            with self.subTest(name):
                script = self.fresh(read=read)
                with script.installed():
                    one = self.cancel()
                    after_one = (one.status_code, one.json().get("detail"), one.headers.get("retry-after"),
                                 list(script.calls), self.subscriber()[0])
                    script.read = None  # the read recovered; the person clicks again
                    two = self.cancel()
                # Red on 61e4abc: no read, the DELETE goes out at once: (200, None, None, ["DELETE"], True).
                self.assertEqual(after_one, (status, api.PROVIDER_SILENT, "60" if status == 503 else None, ["GET"], False))
                self.assertEqual(((two.status_code, two.json()), script.calls), (self.PENDING, ["GET", "GET", "DELETE"]),
                                 two.text)
                self.assertEqual(script.params, [self.DELETE_PARAMS])
                self.assert_access_untouched(claim=True)

    def test_c7_accepted_cancel_then_more_clicks_send_one_delete(self):
        # Guard (green on 61e4abc): after a 2xx the claim stays, further clicks never reach Lava.
        script = self.fresh()
        answers, seen = [], []
        with script.installed():
            for _ in range(3):
                r = self.cancel()
                answers.append((r.status_code, r.json()))
                seen.append(len(script.calls))
        self.assertEqual(answers, [self.PENDING] * 3)
        self.assertEqual((script.deletes, script.calls[-1], seen[0] == seen[1] == seen[2]), (1, "DELETE", True))
        self.assertEqual(script.params, [self.DELETE_PARAMS])
        self.assert_access_untouched(claim=True)

    def test_c11a_failure_diagnostics_exclude_key_contract_email_and_provider_text(self):
        # Guard (green on 61e4abc): only the fixed fields reach the journal and the answer.
        canary = "canary-lava-key-and-provider-text-5e1c"

        def refusal(status):
            return lambda: httpx.Response(status, json={"error": canary, "contractId": CONTRACT,
                                                        "email": "a@example.test", "detail": canary})
        for name, script_args in (("DELETE 403", {"first": refusal(403)}), ("read 400", {"read": refusal(400)})):
            with self.subTest(name), patch.dict(os.environ, {"LAVA_API_KEY": canary}):
                script = self.fresh(**script_args)
                with script.installed(), captured_logs() as records:
                    r = self.cancel()
                output = "\n".join(record.getMessage() for record in records) + "\n" + r.text
                for private in (canary, CONTRACT, "a@example.test"):
                    self.assertNotIn(private, output)
                operations = []
                for record in records:
                    message = record.getMessage()
                    self.assertIsNone(record.exc_info)
                    if message.startswith("Lava request failed "):
                        data = json.loads(message.split("Lava request failed ", 1)[1])
                        self.assertEqual(set(data), FAILURE_KEYS)
                        operations.append(data["operation"])
                    elif message.startswith(CLAIM_NOT_RELEASED):
                        self.assertEqual(set(json.loads(message[len(CLAIM_NOT_RELEASED):])), {"exception_class"})
                if name == "DELETE 403":
                    self.assertEqual(operations, ["subscription_cancel"])

    def test_c11b_claim_release_database_failure_keeps_provider_error_and_logs_one_line(self):
        script = self.fresh(first=RATE_LIMIT)
        real_locked_user = billing.locked_user

        def locked_user(db, user_id):
            if script.deletes:  # the transport already saw the DELETE: releasing the claim hits a dead database
                raise OperationalError("SELECT users FOR UPDATE", {}, Exception("synthetic: database unavailable"))
            return real_locked_user(db, user_id)
        with script.installed(), patch("modules.lava_billing.locked_user", side_effect=locked_user), \
                captured_logs() as records:
            r = self.cancel()
        lines = [json.loads(m[len(CLAIM_NOT_RELEASED):]) for m in (record.getMessage() for record in records)
                 if m.startswith(CLAIM_NOT_RELEASED)]
        # Red on 61e4abc: the claim is never released, so there is no line: (503, ..., "60", 1, []).
        self.assertEqual((r.status_code, r.json().get("detail"), r.headers.get("retry-after"), script.deletes, lines),
                         (503, api.PROVIDER_SILENT, "60", 1, [{"exception_class": "OperationalError"}]), r.text)
        self.assertTrue(self.subscriber()[0])  # the claim could not be released (DESIGN §7.2)
