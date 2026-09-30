"""Disposable PostgreSQL migration, event races and provider uniqueness."""
import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from threading import Barrier
from unittest.mock import patch

from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError
import test_postgres_usage as pg
from database.queries import User, PaddleEvent, LavaCheckout, GPTUsage
from modules import lava_billing as billing
from test_lava import OFFER, PRODUCT, CONTRACT, ENV, iso


@unittest.skipUnless(os.getenv("USAGE_TEST_POSTGRES_URL"), "Local PostgreSQL test URL not configured")
class LavaPostgresTests(unittest.TestCase):
    snapshot = pg.PostgreSQLUsageTests.snapshot
    cleanup_schema = pg.PostgreSQLUsageTests.cleanup_schema

    def apply_script(self, script):
        if "CREATE TABLE lava_checkouts" in script:
            return  # Test the exact migration with pre-existing Paddle rows below.
        pg.PostgreSQLUsageTests.apply_script(self, script)

    def setUp(self):
        pg.PostgreSQLUsageTests.setUp(self)
        with self.engine.begin() as conn:
            conn.exec_driver_sql("UPDATE users SET payment_provider='paddle', provider_customer_id='customer', provider_subscription_id='subscription' WHERE id=1")
            conn.exec_driver_sql("INSERT INTO paddle_events VALUES ('old-event', 'subscription.updated', now(), now(), 'processed')")

    def migrate(self, script=None):
        pg.PostgreSQLUsageTests.apply_script(self, script or self.lava_script)

    def test_migration_preserves_paddle_and_scopes_constraints_and_event_ids(self):
        self.migrate()
        self.assertEqual(self.snapshot(), self.before)
        with self.sessions() as db:
            u = db.get(User, 1)
            self.assertEqual((u.payment_provider, u.provider_customer_id, u.provider_subscription_id), ("paddle", "customer", "subscription"))
            self.assertEqual(db.get(PaddleEvent, "old-event").outcome, "processed")
            u2 = db.get(User, 2); u2.payment_provider = "lava"
            u2.provider_customer_id, u2.provider_subscription_id = "customer", "subscription"
            db.add(PaddleEvent(provider="lava", event_id="lava:old-event", event_type="test", occurred_at=datetime.utcnow()))
            db.commit()
            u2.payment_provider = "paddle"
            with self.assertRaises(IntegrityError): db.commit()
            db.rollback()
        with self.engine.connect() as conn:
            constraints = {c["name"] for c in inspect(conn).get_unique_constraints("users")}
            self.assertIn("uq_users_provider_subscription", constraints)
            self.assertNotIn("users_provider_subscription_id_key", constraints)
        with self.engine.begin() as conn:
            # Previous deployed adapter can still insert/deduplicate after migration.
            conn.exec_driver_sql("INSERT INTO paddle_events (event_id,event_type,occurred_at,processed_at,outcome) VALUES ('old-event','test',now(),now(),'processed') ON CONFLICT(event_id) DO NOTHING")
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
            self.assertEqual(db.query(PaddleEvent).filter_by(provider="lava").count(), 1)
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
