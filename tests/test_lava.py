"""Fake Lava credentials/responses only. Never calls a real payment service."""
import copy
import os
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch, Mock
from uuid import uuid4

import httpx
from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError
import test_my_charts as fixtures
from database.queries import User, LavaCheckout, PaddleCheckout, PaddleEvent, GPTUsage
from modules import lava_billing as billing, lava_client as api, usage

OFFER, PRODUCT, CONTRACT, RENEWAL = [str(uuid4()) for _ in range(4)]
SECRET = "isolated-lava-webhook-secret-for-tests"
ENV = {"LAVA_API_KEY": "fake-test-key", "LAVA_WEBHOOK_SECRET": SECRET, "LAVA_OFFER_ID": OFFER,
       "LAVA_SUCCESS_RETURN_URL": "https://example.test/my-charts?billing=lava",
       "LAVA_FAILURE_RETURN_URL": "https://example.test/my-charts?billing=lava",
       "LAVA_CANCEL_RETURN_URL": "https://example.test/my-charts?billing=lava"}


def iso(value):
    return value.isoformat() + "Z"


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

    def test_failed_checkout_allows_retry_and_paddle_switch(self):
        from modules import paddle_billing
        self.snapshot.update(status="FAILED", subscriptionStatus="FAILED", expiredAt=None)
        self.assertEqual(self.send(self.event("payment.failed")).status_code, 200)
        with patch.object(paddle_billing, "premium_price_id", return_value="price"), patch.object(paddle_billing, "paddle_client") as client:
            client.return_value.transactions.create.return_value.id = "txn_test"
            r = self.client.post("/payments/paddle/checkout", headers=self.owner)
            self.assertEqual(r.status_code, 200, r.text)
            client.return_value.transactions.create.assert_called_once()
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
        from modules import paddle_billing
        self.snapshot.update(status="FAILED", subscriptionStatus="FAILED", expiredAt=None)
        with self.sessions() as db:
            u = db.get(User, 1)
            u.payment_provider, u.provider_subscription_id, u.subscription_status = "lava", CONTRACT, "failed"
            db.get(LavaCheckout, "bound").state = "failed"
            db.commit()
        with patch.object(paddle_billing, "premium_price_id", return_value="price"), patch.object(paddle_billing, "paddle_client") as client:
            client.return_value.transactions.create.return_value.id = "txn_test"
            r = self.client.post("/payments/paddle/checkout", headers=self.owner)
            self.assertEqual(r.status_code, 200, r.text)

    def test_reconciliation_failure_keeps_intent_and_never_creates_invoice(self):
        self.network.side_effect = HTTPException(502, "unavailable")
        self.assertEqual(self.client.post("/payments/lava/checkout", headers=self.owner).status_code, 502)
        with self.sessions() as db:
            self.assertEqual(db.get(LavaCheckout, "bound").state, "ready")
            self.assertEqual(db.query(LavaCheckout).count(), 1)
        self.assertFalse(any(c.args[0] == "POST" for c in self.network.call_args_list))

    def test_pending_lava_allows_paddle_but_paid_lava_blocks_new_checkout(self):
        from modules import paddle_billing
        with patch.object(paddle_billing, "premium_price_id", return_value="price"), patch.object(paddle_billing, "paddle_client") as client:
            client.return_value.transactions.create.return_value.id = "txn_test"
            self.assertEqual(self.client.post("/payments/paddle/checkout", headers=self.owner).status_code, 200)
            self.send(self.event())
            self.assertEqual(self.client.post("/payments/paddle/checkout", headers=self.owner).status_code, 409)
            self.assertEqual(self.client.post("/payments/lava/checkout", headers=self.owner).status_code, 409)
            client.return_value.transactions.create.assert_called_once()

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
                self.assertFalse(db.get(PaddleEvent, "lava:" + value["event_id"]).details["subscription_cancelled"])

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
            user.payment_provider = "paddle"; user.provider_subscription_id = CONTRACT
            usage.apply_plan(db, user, "premium", self.start, self.end); db.commit()
        self.assertEqual(self.send(self.event()).json()["outcome"], "billing_conflict")
        self.assertEqual(self.client.post("/payments/lava/checkout", headers=self.owner).status_code, 409)
        self.assertEqual(self.client.post("/payments/lava/cancel", headers=self.owner).status_code, 409)
        with self.sessions() as db: self.assertEqual(db.get(User, 1).payment_provider, "paddle")

    def test_unique_ids_scoped_and_same_provider_duplicate_denied(self):
        with self.sessions() as db:
            for uid, provider in ((1, "paddle"), (2, "lava")):
                user = db.get(User, uid); user.payment_provider = provider
                user.provider_customer_id = "same"; user.provider_subscription_id = CONTRACT
            db.commit()
            db.get(User, 2).payment_provider = "paddle"
            with self.assertRaises(IntegrityError): db.commit()
            db.rollback()
            for provider in ("paddle", "lava"):
                db.add(PaddleEvent(provider=provider, event_id="same" if provider == "paddle" else "lava:same", event_type="test", occurred_at=self.start))
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
        with self.sessions() as db: self.assertEqual(db.query(PaddleEvent).count(), 0)

    def test_api_eventual_consistency_does_not_acknowledge_unconfirmed_payment(self):
        self.snapshot["status"] = "IN_PROGRESS"
        value = self.event()
        self.assertEqual(self.send(value).status_code, 503)
        self.snapshot["status"] = "COMPLETED"
        self.assertEqual(self.send(value).status_code, 200)
        self.assertEqual(self.state()["plan"], "premium")

    def test_paddle_pending_checkout_allows_lava(self):
        with self.sessions() as db:
            db.add(PaddleCheckout(user_id=2, state="creating")); db.commit()
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
    def test_failure_diagnostics_exclude_provider_data_and_do_not_retry(self):
        import json
        original = httpx.Client
        secret = "sensitive-key-email-signature-body"
        cases = [
            (httpx.ReadTimeout(secret), "request", "ReadTimeout", None),
            (httpx.Response(200, content=secret.encode()), "decode_response", "JSONDecodeError", 200),
            (httpx.Response(500, content=secret.encode()), "http_status", "HTTPException", 500),
            (httpx.Response(429, content=secret.encode()), "http_status", "HTTPException", 429),
        ]
        for response, stage, exception, status in cases:
            with self.subTest(status=status, stage=stage), patch.dict(os.environ, {**ENV, "LAVA_API_KEY": secret}):
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
