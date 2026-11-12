"""Disposable PostgreSQL migration, event races and provider uniqueness."""
import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from threading import Barrier, Event
from unittest.mock import patch

import httpx
from fastapi import HTTPException
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError
import test_postgres_usage as pg
from database.queries import User, BillingEvent, LavaCheckout, GPTUsage
from modules import lava_billing as billing
from test_lava import OFFER, PRODUCT, CONTRACT, ENV, iso, InvoiceScript, CancelScript

WAIT = 5  # seconds: every barrier/event wait is bounded; a broken ordering fails, it never hangs


@unittest.skipUnless(os.getenv("USAGE_TEST_POSTGRES_URL"), "Local PostgreSQL test URL not configured")
class LavaPostgresTests(unittest.TestCase):
    snapshot = pg.PostgreSQLUsageTests.snapshot
    cleanup_schema = pg.PostgreSQLUsageTests.cleanup_schema

    def apply_script(self, script):
        if "CREATE TABLE lava_checkouts" in script or "RENAME TO billing_events" in script:
            return  # Test the exact migration with pre-existing Paddle rows below.
        pg.PostgreSQLUsageTests.apply_script(self, script)

    def setUp(self):
        pg.PostgreSQLUsageTests.setUp(self)
        with self.engine.begin() as conn:
            conn.exec_driver_sql("UPDATE users SET payment_provider='paddle', provider_customer_id='customer', provider_subscription_id='subscription' WHERE id=1")
            conn.exec_driver_sql("INSERT INTO paddle_events VALUES ('old-event', 'subscription.updated', now(), now(), 'processed')")

    def migrate(self, script=None):
        pg.PostgreSQLUsageTests.apply_script(self, script or self.lava_script)
        pg.PostgreSQLUsageTests.apply_script(self, self.stripe_script)

    def test_migration_preserves_paddle_and_scopes_constraints_and_event_ids(self):
        self.migrate()
        self.assertEqual(self.snapshot(), self.before)
        with self.sessions() as db:
            u = db.get(User, 1)
            self.assertEqual((u.payment_provider, u.provider_customer_id, u.provider_subscription_id), ("paddle", "customer", "subscription"))
            self.assertEqual(db.get(BillingEvent, "old-event").outcome, "processed")
            u2 = db.get(User, 2); u2.payment_provider = "lava"
            u2.provider_customer_id, u2.provider_subscription_id = "customer", "subscription"
            db.add(BillingEvent(provider="lava", event_id="lava:old-event", event_type="test", occurred_at=datetime.utcnow()))
            db.commit()
            u2.payment_provider = "paddle"
            with self.assertRaises(IntegrityError): db.commit()
            db.rollback()
        with self.engine.connect() as conn:
            constraints = {c["name"] for c in inspect(conn).get_unique_constraints("users")}
            self.assertIn("uq_users_provider_subscription", constraints)
            self.assertNotIn("users_provider_subscription_id_key", constraints)
        with self.engine.begin() as conn:
            # Retained historical event IDs still deduplicate after the rename.
            conn.exec_driver_sql("INSERT INTO billing_events (provider,event_id,event_type,occurred_at,processed_at,outcome) VALUES ('paddle','old-event','test',now(),now(),'processed') ON CONFLICT(event_id) DO NOTHING")
        with self.assertRaises(Exception): self.migrate()

    def test_failed_migration_rolls_back_constraints_and_keeps_paddle(self):
        with self.assertRaises(Exception): self.migrate(self.lava_script.replace("COMMIT;", "SELECT 1/0; COMMIT;"))
        with self.engine.connect() as conn:
            self.assertNotIn("lava_checkouts", inspect(conn).get_table_names())
            self.assertIn("users_provider_subscription_id_key", {c["name"] for c in inspect(conn).get_unique_constraints("users")})
            self.assertEqual(conn.exec_driver_sql("SELECT event_id FROM paddle_events").scalar_one(), "old-event")
        self.migrate()

    def test_preflight_refuses_unowned_ids_without_guessing(self):
        with self.engine.begin() as conn:
            conn.exec_driver_sql("UPDATE users SET payment_provider=NULL WHERE id=1")
        with self.assertRaises(Exception): self.migrate()
        with self.engine.connect() as conn:
            self.assertNotIn("lava_checkouts", inspect(conn).get_table_names())

    def prepare_race(self):
        self.migrate()
        self.start = datetime.utcnow() - timedelta(days=2)
        with self.sessions() as db:
            user = db.get(User, 1)
            user.payment_provider = user.provider_customer_id = user.provider_subscription_id = None
            db.add(LavaCheckout(user_id=1, contract_id=CONTRACT, offer_id=OFFER, product_id=PRODUCT,
                buyer_email="test@example.com", state="ready", created_at=self.start - timedelta(seconds=1)))
            db.commit()
        self.value = {"eventType": "payment.success", "contractId": CONTRACT, "timestamp": iso(self.start),
            "product": {"id": PRODUCT}, "buyer": {"email": "test@example.com"}, "status": "subscription-active",
            "currency": "RUB", "amount": 799}
        self.provider_state = dict(paid=self.start, end=self.start + timedelta(days=31), cancelled=None,
                                   terminated=None, status="ACTIVE", completed_ids={CONTRACT})

    def test_concurrent_duplicate_commits_one_event_and_one_period(self):
        self.prepare_race()
        barrier = Barrier(2)
        def state(_):
            barrier.wait(timeout=5)
            return self.provider_state.copy()
        def run(_):
            with self.sessions() as db: return billing.process_event(db, self.value)
        with patch.object(billing, "snapshot", side_effect=state), ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(run, range(2)))
        self.assertEqual(sorted(results), ["duplicate", "processed"])
        with self.sessions() as db:
            self.assertEqual(db.query(BillingEvent).filter_by(provider="lava").count(), 1)
            self.assertEqual(db.get(User, 1).current_period_start, self.start)
            self.assertEqual(db.query(GPTUsage).count(), 2)  # existing natal quota preserved

    def test_concurrent_cancel_and_active_snapshot_cannot_restore_canceled_access(self):
        self.prepare_race()
        barrier = Barrier(2)
        canceled = {**self.value, "eventType": "subscription.cancelled",
                    "cancelledAt": iso(self.start + timedelta(seconds=1)), "willExpireAt": iso(self.provider_state["end"])}
        def run(value):
            # Thread-local return state through the single shared snapshot mock.
            with self.sessions() as db: return billing.process_event(db, value)
        import threading
        local = threading.local()
        def fetch(_):
            barrier.wait(timeout=5)
            return local.state
        def worker(value):
            local.state = self.provider_state.copy()
            if value["eventType"] == "subscription.cancelled":
                local.state.update(status="CANCELLED", cancelled=self.start + timedelta(seconds=1))
            return run(value)
        with patch.object(billing, "snapshot", side_effect=fetch), ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(worker, (self.value, canceled)))
        with self.sessions() as db:
            self.assertEqual(db.get(User, 1).scheduled_cancel_at, self.provider_state["end"])

    def test_concurrent_checkout_creates_only_one_remote_invoice(self):
        self.migrate()
        barrier = Barrier(2)
        def offer(_):
            barrier.wait(timeout=5)
            return PRODUCT
        def run(_):
            from fastapi import HTTPException
            with self.sessions() as db:
                try:
                    billing.create_checkout(db, 2)
                    return 200
                except HTTPException as exc:
                    return exc.status_code
        response = {"id": CONTRACT, "paymentUrl": "https://pay.example.test/invoice",
                    "amountTotal": {"currency": "RUB", "amount": 799}}
        with patch.dict(os.environ, ENV), patch.object(billing.api, "verified_offer", side_effect=offer), \
                patch.object(billing.api, "request", return_value=response) as network, ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(run, range(2)))
        self.assertEqual(sorted(results), [200, 409])
        self.assertEqual(network.call_count, 1)
        with self.sessions() as db:
            self.assertEqual(db.query(LavaCheckout).filter_by(user_id=2).count(), 1)

    # --- PAY-LAVA-STUCK T8-T10 (tasks/PAY-LAVA-STUCK/DESIGN.md §3 "Гонка двух вкладок", §5) -------------
    # Real create_checkout + real lava_client.request over httpx.MockTransport; row locks are PostgreSQL's.
    def pay(self):
        """One tab of user 2 (other@example.com): (status, detail, headers) of the checkout call."""
        with self.sessions() as db:
            try:
                return 200, billing.create_checkout(db, 2)["url"], None
            except HTTPException as exc:
                return exc.status_code, exc.detail, exc.headers

    def attempts(self):
        with self.sessions() as db:
            rows = db.query(LavaCheckout).filter_by(user_id=2).all()
            return sorted(((c.state, c.contract_id, c.payment_url) for c in rows), key=lambda row: row[0])

    def tab_b_during_held_invoice_post(self, outcome):
        """Tab A is stopped inside its invoice POST (its `creating` row is committed, no lock held);
        tab B pays; then A gets `outcome`; then tab C pays. Ordering by Events, never by sleep."""
        self.migrate()
        posting, release = Event(), Event()

        def hold():
            posting.set()
            if not release.wait(WAIT):
                raise RuntimeError("tab A was never released")
        script = InvoiceScript(outcome, before_first=hold)
        with patch.dict(os.environ, ENV), script.installed(), ThreadPoolExecutor(max_workers=1) as pool:
            try:
                tab_a = pool.submit(self.pay)
                self.assertTrue(posting.wait(WAIT), "tab A never reached the invoice POST")
                tab_b = self.pay()
                posts_while_held = script.posts
            finally:
                release.set()
            a = tab_a.result(timeout=WAIT)
            after_a = self.attempts()
            c = self.pay()
        return script, a, tab_b, posts_while_held, after_a, c

    def test_t8_second_tab_during_refused_invoice_then_new_payment(self):
        script, a, b, posts_while_held, after_a, c = self.tab_b_during_held_invoice_post(
            lambda: httpx.Response(429, json={}))
        self.assertEqual(b, (409, billing.CHECKOUT_PENDING, None))  # B never posts while A's outcome is open
        self.assertEqual(posts_while_held, 1)
        self.assertEqual(a, (503, billing.api.PROVIDER_SILENT, {"Retry-After": "60"}))
        # Red on 1c6bb25: A's attempt stays `creating`, so C gets 409 and no second POST happens.
        self.assertEqual((after_a, c[0], script.posts), ([("rejected", None, None)], 200, 2), c)
        self.assertEqual([row[0] for row in self.attempts()], ["ready", "rejected"])

    def test_t9_after_refusal_two_simultaneous_tabs_create_one_invoice(self):
        self.migrate()
        script = InvoiceScript(lambda: httpx.Response(429, json={}))
        barrier = Barrier(2)
        real_offer = billing.api.verified_offer

        def offer(offer_id):
            barrier.wait(timeout=WAIT)  # both tabs pass the catalog check before either locks the user
            return real_offer(offer_id)
        with patch.dict(os.environ, ENV), script.installed():
            first = self.pay()
            with patch.object(billing.api, "verified_offer", side_effect=offer), \
                    ThreadPoolExecutor(max_workers=2) as pool:
                results = sorted(status for status, _, _ in pool.map(lambda _: self.pay(), range(2)))
        self.assertEqual(first[0], 503)
        # Red on 1c6bb25: both tabs get 409 at reconciliation because the refused attempt is still `creating`.
        self.assertEqual((results, script.posts), ([200, 409], 2))
        self.assertEqual([row[0] for row in self.attempts()], ["ready", "rejected"])

    def test_t10_second_tab_during_unclear_invoice_stays_blocked(self):
        # Guard (green on 1c6bb25): a read timeout after the POST was sent is an unclear outcome.
        script, a, b, posts_while_held, after_a, c = self.tab_b_during_held_invoice_post(
            lambda: httpx.ReadTimeout("synthetic: read timeout"))
        self.assertEqual(b, (409, billing.CHECKOUT_PENDING, None))
        self.assertEqual(posts_while_held, 1)
        self.assertEqual(a, (502, billing.api.PROVIDER_SILENT, None))
        self.assertEqual((after_a, c, script.posts),
                         ([("creating", None, None)], (409, billing.CHECKOUT_PENDING, None), 1))

    # --- PAY-LAVA-CANCEL C8-C10 (tasks/PAY-LAVA-CANCEL/DESIGN.md §3 "Двойное нажатие и две вкладки", §5) -------
    # Real cancel_subscription + real lava_client.request over httpx.MockTransport; row locks are PostgreSQL's.
    PENDING = (200, {"pending": True}, None)
    DELETE_PARAMS = {"contractId": CONTRACT, "email": "test@example.com"}

    def prepare_subscriber(self):
        """User 1 (test@example.com) holds an active Lava subscription from a verified payment event."""
        self.prepare_race()
        with patch.object(billing, "snapshot", return_value=self.provider_state.copy()), self.sessions() as db:
            self.assertEqual(billing.process_event(db, self.value), "processed")
        self.subscription = {"id": CONTRACT, "datetime": iso(self.start), "status": "COMPLETED",
            "receipt": {"amount": 799, "currency": "RUB"}, "buyer": {"email": "test@example.com"},
            "periodicity": "MONTHLY", "subscriptionStatus": "ACTIVE", "expiredAt": iso(self.provider_state["end"]),
            "cancelledAt": None, "terminatedAt": None, "recurrentPayments": []}
        self.release_claim()

    def subscriber(self):
        """(claim, plan, subscription_status, scheduled_cancel_at)."""
        with self.sessions() as db:
            user = db.get(User, 1)
            checkout = db.query(LavaCheckout).filter_by(user_id=1, contract_id=CONTRACT).one()
            return checkout.cancel_requested, user.plan, user.subscription_status, user.scheduled_cancel_at

    def release_claim(self):
        """Clean state for every case: no claim, the same paid subscriber."""
        with self.sessions() as db:
            db.query(LavaCheckout).filter_by(user_id=1, contract_id=CONTRACT).one().cancel_requested = False
            db.commit()
        self.assertEqual(self.subscriber(), (False, "premium", "active", None))

    def cancel_tab(self):
        """One tab of user 1: (status, body or detail, headers). A broken ordering is a result, not a hang."""
        with self.sessions() as db:
            try:
                return 200, billing.cancel_subscription(db, 1), None
            except HTTPException as exc:
                return exc.status_code, exc.detail, exc.headers
            except Exception as exc:  # BrokenBarrierError / RuntimeError from a bounded wait in the transport
                return "error", type(exc).__name__, None

    def tab_b_during_held_delete(self, outcome, applied=False):
        """Tab A is stopped inside its DELETE (its claim is committed, no lock held); tab B cancels;
        then A gets `outcome`; then tab C cancels. Ordering by Events, never by sleep."""
        deleting, release = Event(), Event()

        def hold():
            deleting.set()
            if not release.wait(WAIT):
                raise RuntimeError("tab A was never released")
        script = CancelScript(self.subscription, first=outcome, applied=applied, before_first=hold)
        with patch.dict(os.environ, ENV), script.installed(), ThreadPoolExecutor(max_workers=1) as pool:
            try:
                tab_a = pool.submit(self.cancel_tab)
                self.assertTrue(deleting.wait(WAIT), "tab A never reached the DELETE")
                before_b = len(script.calls)
                b = self.cancel_tab()
                calls_during_b = script.calls[before_b:]
            finally:
                release.set()
            a = tab_a.result(timeout=WAIT)
            claim_after_a = self.subscriber()[0]
            before_c = len(script.calls)
            c = self.cancel_tab()
            calls_of_c = script.calls[before_c:]
        return script, a, (b, calls_during_b), claim_after_a, (c, calls_of_c)

    def test_c8_second_tab_during_refused_delete_then_new_cancel(self):
        self.prepare_subscriber()
        script, a, b, claim_after_a, c = self.tab_b_during_held_delete(lambda: httpx.Response(429, json={}))
        self.assertEqual(b, (self.PENDING, []))  # B answers "sent" from A's claim and never calls Lava
        self.assertEqual(a, (503, billing.api.PROVIDER_SILENT, {"Retry-After": "60"}))
        # Red on 61e4abc: A's claim stays true, so C answers "pending" without Lava: (True, (PENDING, []), 1).
        self.assertEqual((claim_after_a, c, script.deletes), (False, (self.PENDING, ["GET", "DELETE"]), 2))
        self.assertEqual(script.params, [self.DELETE_PARAMS] * 2)
        self.assertEqual(self.subscriber(), (True, "premium", "active", None))

    def test_c9_two_simultaneous_tabs_send_one_delete(self):
        # Guard (green on 61e4abc): both tabs pass the read together, one claim wins, one DELETE.
        # A lock held across the HTTP read would stop the second tab before its read: the barrier breaks in 5 s.
        self.prepare_subscriber()
        barrier = Barrier(2)
        script = CancelScript(self.subscription, before_read=lambda: barrier.wait(timeout=WAIT))
        with patch.dict(os.environ, ENV), script.installed(), ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.cancel_tab(), range(2)))
        self.assertEqual((results, script.deletes, self.subscriber()[0]), ([self.PENDING] * 2, 1, True))
        self.assertEqual(script.params, [self.DELETE_PARAMS])
        self.assertEqual(self.subscriber()[1:], ("premium", "active", None))

    def test_c10_second_tab_during_unclear_delete_then_new_cancel_reads_first(self):
        self.prepare_subscriber()
        for applied in (False, True):
            with self.subTest(applied="applied" if applied else "not applied"):
                self.release_claim()
                script, a, b, claim_after_a, c = self.tab_b_during_held_delete(
                    lambda: httpx.ReadTimeout("synthetic: read timeout"), applied=applied)
                self.assertEqual(b, (self.PENDING, []))
                self.assertEqual(a, (502, billing.api.PROVIDER_SILENT, None))  # never shown as "cancelled"
                if applied:
                    # Guard (green on 61e4abc): Lava already cancelled, so no second DELETE whatever the claim says.
                    self.assertEqual((c[0], script.deletes), (self.PENDING, 1))
                else:
                    # Red on 61e4abc: A's claim stays true, C answers "pending" without Lava: (True, (PENDING, []), 1).
                    self.assertEqual((claim_after_a, c, script.deletes), (False, (self.PENDING, ["GET", "DELETE"]), 2))
                    self.assertEqual(script.params, [self.DELETE_PARAMS] * 2)
                self.assertEqual(self.subscriber()[1:], ("premium", "active", None))
