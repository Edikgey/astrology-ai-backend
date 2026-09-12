"""Signed HTTP webhooks and mocked SDK calls; no Paddle/API credentials needed."""
import copy
import hashlib
import hmac
import json
import os
import time
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch, Mock
from uuid import uuid4
from sqlalchemy import event as sql_event
import test_my_charts as fixtures
from database.queries import User, PaddleCheckout, PaddleEvent, GPTUsage
from modules import usage

PRICE = "pri_01m2aaqxhr6prath62z1efsvvn"
SUB = "sub_" + "a" * 26
CUSTOMER = "ctm_" + "a" * 26
TRANSACTION = "txn_" + "a" * 26
SECRET = "isolated-webhook-test-secret"


class PaddleTests(unittest.TestCase):
    tearDown = fixtures.MyChartsTests.tearDown
    chart = fixtures.MyChartsTests.chart

    def setUp(self):
        fixtures.MyChartsTests.setUp(self)
        env = patch.dict(os.environ, {"PADDLE_WEBHOOK_SECRET": SECRET, "PADDLE_PREMIUM_PRICE_ID": PRICE})
        env.start(); self.addCleanup(env.stop)
        self.start = datetime.utcnow() - timedelta(days=1)
        self.end = self.start + timedelta(days=30)
        self.binding = str(uuid4())
        with self.sessions() as db:
            db.add(PaddleCheckout(id=self.binding, user_id=1, transaction_id=TRANSACTION, state="ready"))
            db.commit()

    def payload(self, status="active", **changes):
        data = {"id": SUB, "customer_id": CUSTOMER, "status": status,
            "custom_data": {"user_id": "1", "checkout_binding": self.binding},
            "items": [{"quantity": 1, "price": {"id": PRICE}}],
            "billing_cycle": {"interval": "month", "frequency": 1},
            "current_billing_period": {"starts_at": self.start.isoformat() + "Z", "ends_at": self.end.isoformat() + "Z"},
            "scheduled_change": None}
        data.update(changes)
        return {"event_id": "evt_" + uuid4().hex[:26], "event_type": "subscription.updated",
            "occurred_at": datetime.utcnow().isoformat() + "Z", "data": data}

    def send(self, payload, timestamp=None, secret=SECRET, body=None):
        raw = body if body is not None else json.dumps(payload, ensure_ascii=False).encode()
        ts = str(int(time.time()) if timestamp is None else timestamp)
        signature = hmac.new(secret.encode(), ts.encode() + b":" + raw, hashlib.sha256).hexdigest()
        return self.client.post("/payments/paddle/webhook", content=raw,
            headers={"Paddle-Signature": f"ts={ts};h1={signature}", "Content-Type": "application/json"})

    def state(self):
        r = self.client.get("/account/usage", headers=self.owner)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def test_invalid_missing_stale_future_and_malformed_signatures(self):
        payload = self.payload()
        for response in [self.send(payload, secret="wrong"), self.send(payload, timestamp=int(time.time())-30),
                         self.send(payload, timestamp=int(time.time())+30),
                         self.client.post("/payments/paddle/webhook", json=payload),
                         self.client.post("/payments/paddle/webhook", json=payload, headers={"Paddle-Signature": "ts=bad;h1=bad"})]:
            self.assertEqual(response.status_code, 401, response.text)
        self.assertEqual(self.state()["plan"], "free")
        with self.sessions() as db: self.assertEqual(db.query(PaddleEvent).count(), 0)

    def test_activation_is_verified_without_jwt_and_duplicate_does_not_reset_usage(self):
        payload = self.payload(); payload["event_type"] = "subscription.created"
        response = self.send(payload)
        self.assertEqual(response.status_code, 200, response.text)
        with self.sessions() as db:
            db.add(GPTUsage(user_id=1, chart_id=123, status="succeeded", plan="premium",
                period_start=self.start, period_end=self.end)); db.commit()
        response = self.send(payload)
        self.assertEqual(response.json()["outcome"], "duplicate")
        state = self.state()
        self.assertEqual((state["plan"], state["gpt_messages_used"], state["gpt_messages_limit"]), ("premium", 1, 300))
        self.assertEqual(state["saved_charts_limit"], 10)
        with self.sessions() as db:
            user = db.get(User, 1)
            self.assertEqual((user.provider_customer_id, user.provider_subscription_id), (CUSTOMER, SUB))
            self.assertEqual(db.get(User, 2).plan, "free")
            self.assertEqual(db.query(PaddleEvent).count(), 1)

    def test_renewal_changes_existing_usage_period_and_stale_event_is_ignored(self):
        original = self.payload(); self.assertEqual(self.send(original).status_code, 200)
        with self.sessions() as db:
            db.add(GPTUsage(user_id=1, chart_id=123, status="succeeded", plan="premium",
                period_start=self.start, period_end=self.end)); db.commit()
        renewed = self.payload(current_billing_period={"starts_at": self.end.isoformat()+"Z",
            "ends_at": (self.end+timedelta(days=30)).isoformat()+"Z"})
        self.assertEqual(self.send(renewed).status_code, 200)
        old = copy.deepcopy(original); old["event_id"] = "evt_" + "z"*26
        self.assertEqual(self.send(old).json()["outcome"], "ignored_stale")
        with patch("modules.usage.utcnow", return_value=self.end+timedelta(seconds=1)):
            state = self.state()
        self.assertEqual(state["gpt_messages_used"], 0)
        self.assertEqual(state["current_period_start"], self.end.isoformat())

    def test_scheduled_cancel_preserves_premium_until_end_even_if_final_webhook_is_late(self):
        payload = self.payload(scheduled_change={"action": "cancel", "effective_at": self.end.isoformat()+"Z"})
        self.assertEqual(self.send(payload).status_code, 200)
        self.assertEqual(self.state()["plan"], "premium")
        self.assertTrue(self.state()["cancel_at_period_end"])
        with patch("modules.usage.utcnow", return_value=self.end+timedelta(seconds=1)):
            self.assertEqual(self.state()["plan"], "free")

    def test_canceled_revokes_access_without_deleting_charts_or_ids(self):
        self.send(self.payload())
        for _ in range(4): self.chart(related=False)
        response = self.send(self.payload("canceled", current_billing_period=None))
        self.assertEqual(response.status_code, 200, response.text)
        state = self.state()
        self.assertEqual((state["plan"], state["saved_charts_used"], state["saved_charts_limit"]), ("free",4,3))
        with self.sessions() as db: self.assertEqual(db.get(User,1).provider_subscription_id, SUB)
        self.assertEqual(self.client.post("/natal-chart", headers=self.owner, json=fixtures.PAYLOAD).status_code,409)

    def test_past_due_retains_confirmed_period_and_does_not_grant_unpaid_renewal(self):
        self.send(self.payload())
        response = self.send(self.payload("past_due", current_billing_period={
            "starts_at":self.end.isoformat()+"Z", "ends_at":(self.end+timedelta(days=30)).isoformat()+"Z"}))
        self.assertEqual(response.status_code,200,response.text)
        state=self.state()
        self.assertEqual(state["plan"],"premium")
        self.assertEqual(state["current_period_start"],self.start.isoformat())
        with patch("modules.usage.utcnow",return_value=self.end+timedelta(seconds=1)):
            self.assertFalse(self.state()["gpt_period_valid"])

    def test_unknown_user_spoofed_user_and_unbound_checkout_cannot_grant_access(self):
        for custom in ({"user_id":"999","checkout_binding":self.binding},
                       {"user_id":"2","checkout_binding":self.binding}, {"user_id":"1"},
                       {"user_id":"1","checkout_binding":str(uuid4())}):
            response=self.send(self.payload(custom_data=custom))
            self.assertEqual(response.status_code,400,response.text)
        with self.sessions() as db:
            self.assertEqual([u.plan for u in db.query(User).order_by(User.id)], ["free","free"])
            self.assertEqual(db.query(PaddleEvent).count(),0)

    def test_wrong_price_or_trialing_never_grants_premium(self):
        self.assertEqual(self.send(self.payload(items=[{"quantity":1,"price":{"id":"pri_"+"b"*26}}])).status_code,200)
        self.assertEqual(self.state()["plan"],"free")
        self.send(self.payload("trialing"))
        self.assertEqual(self.state()["plan"],"free")

    def test_overlap_is_rejected_atomically_and_event_can_retry(self):
        self.send(self.payload())
        invalid=self.payload(current_billing_period={"starts_at":(self.start+timedelta(hours=1)).isoformat()+"Z",
            "ends_at":(self.end+timedelta(days=1)).isoformat()+"Z"})
        self.assertEqual(self.send(invalid).status_code,400)
        with self.sessions() as db: self.assertIsNone(db.get(PaddleEvent,invalid["event_id"]))
        invalid["data"]["current_billing_period"]["starts_at"]=self.start.isoformat()+"Z"
        self.assertEqual(self.send(invalid).status_code,200)

    def test_database_failure_rolls_back_entitlements_and_event(self):
        def fail(conn,cursor,statement,*args):
            if statement.startswith("UPDATE users"): raise RuntimeError("isolated failure")
        sql_event.listen(self.engine,"before_cursor_execute",fail)
        payload=self.payload()
        try: self.assertEqual(self.send(payload).status_code,500)
        finally: sql_event.remove(self.engine,"before_cursor_execute",fail)
        with self.sessions() as db:
            self.assertEqual(db.get(User,1).plan,"free")
            self.assertEqual(db.query(PaddleEvent).count(),0)
        self.assertEqual(self.send(payload).status_code,200)

    def test_checkout_requires_auth_uses_server_owner_and_reuses_transaction(self):
        sdk=Mock(); sdk.transactions.create.return_value=SimpleNamespace(id="txn_"+"b"*26)
        with patch("modules.paddle_billing.paddle_client",return_value=sdk):
            for headers in ({},self.guest):
                self.assertEqual(self.client.post("/payments/paddle/checkout",headers=headers).status_code,401)
            # User 2 has no pending checkout. Request body cannot choose the owner/price.
            r=self.client.post("/payments/paddle/checkout",headers=self.other,json={"user_id":1,"price_id":"wrong"})
            self.assertEqual(r.status_code,200,r.text)
            operation=sdk.transactions.create.call_args.args[0]
            self.assertEqual(operation.custom_data.data["user_id"],"2")
            self.assertEqual((operation.items[0].price_id,operation.items[0].quantity),(PRICE,1))
            again=self.client.post("/payments/paddle/checkout",headers=self.other)
            self.assertEqual(again.json(),r.json()); sdk.transactions.create.assert_called_once()
            self.assertEqual(self.state()["plan"],"free")

    def test_ambiguous_checkout_failure_does_not_create_another_transaction(self):
        sdk=Mock(); sdk.transactions.create.side_effect=TimeoutError()
        with patch("modules.paddle_billing.paddle_client",return_value=sdk):
            self.assertEqual(self.client.post("/payments/paddle/checkout",headers=self.other).status_code,502)
            self.assertEqual(self.client.post("/payments/paddle/checkout",headers=self.other).status_code,409)
            sdk.transactions.create.assert_called_once()

    def test_portal_uses_only_authenticated_users_provider_ids(self):
        self.send(self.payload())
        sdk=Mock(); sdk.customer_portal_sessions.create.return_value=SimpleNamespace(
            urls=SimpleNamespace(general=SimpleNamespace(overview="https://sandbox-customer-portal.paddle.com/example")))
        with patch("modules.paddle_billing.paddle_client",return_value=sdk):
            self.assertEqual(self.client.post("/payments/paddle/portal",headers=self.other,json={"customer_id":CUSTOMER}).status_code,409)
            r=self.client.post("/payments/paddle/portal",headers=self.owner)
            self.assertEqual(r.status_code,200,r.text)
            self.assertEqual(r.headers["cache-control"],"no-store")
            args=sdk.customer_portal_sessions.create.call_args.args
            self.assertEqual(args[0],CUSTOMER); self.assertEqual(args[1].subscription_ids,[SUB])

    def test_live_key_fails_closed(self):
        with patch.dict(os.environ,{"PADDLE_SANDBOX_API_KEY":"pdl_live_apikey_not-real"}):
            self.assertEqual(self.client.post("/payments/paddle/checkout",headers=self.other).status_code,503)

    def test_second_subscription_and_conflicting_customer_cannot_replace_owner(self):
        self.send(self.payload())
        conflicting=self.payload(customer_id="ctm_"+"b"*26)
        self.assertEqual(self.send(conflicting).status_code,400)
        second=self.payload(id="sub_"+"b"*26)
        self.assertEqual(self.send(second).status_code,400)
        with self.sessions() as db:
            self.assertEqual(db.get(User,1).provider_subscription_id,SUB)
            self.assertEqual(db.get(User,1).provider_customer_id,CUSTOMER)

    def test_resubscribe_after_cancellation_ignores_late_old_subscription(self):
        self.send(self.payload())
        self.send(self.payload("canceled",current_billing_period=None))
        binding=str(uuid4())
        with self.sessions() as db:
            db.add(PaddleCheckout(id=binding,user_id=1,transaction_id="txn_"+"b"*26,state="ready"));db.commit()
        new=self.payload(id="sub_"+"b"*26,custom_data={"user_id":"1","checkout_binding":binding})
        self.assertEqual(self.send(new).status_code,200)
        late=self.payload("canceled",current_billing_period=None)
        self.assertEqual(self.send(late).json()["outcome"],"ignored_old_or_second_subscription")
        self.assertEqual(self.state()["plan"],"premium")

    def test_sdk_transaction_payload_serializes_expected_paddle_fields(self):
        from paddle_billing.Json import PayloadEncoder
        sdk=Mock();sdk.transactions.create.return_value=SimpleNamespace(id="txn_"+"b"*26)
        with patch("modules.paddle_billing.paddle_client",return_value=sdk):
            self.assertEqual(self.client.post("/payments/paddle/checkout",headers=self.other).status_code,200)
        operation=sdk.transactions.create.call_args.args[0]
        serialized=json.loads(json.dumps(operation,cls=PayloadEncoder))
        self.assertEqual(serialized["items"],[{"price_id":PRICE,"quantity":1}])
        self.assertEqual(serialized["custom_data"]["user_id"],"2")
