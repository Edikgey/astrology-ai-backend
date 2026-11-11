"""Opt-in disposable localhost PostgreSQL migration and privacy/concurrency tests."""
import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from unittest.mock import patch
from uuid import uuid4
from sqlalchemy import inspect
import test_postgres_usage as pg
from modules import product_analytics as analytics
from modules.product_analytics import AnalyticsConsent, AnalyticsOutbox, AnalyticsMonthlyBudget
from test_product_analytics import ENV


@unittest.skipUnless(os.getenv('USAGE_TEST_POSTGRES_URL'), 'Disposable localhost PostgreSQL required')
class ProductAnalyticsPostgresTests(unittest.TestCase):
    cleanup_schema = pg.PostgreSQLUsageTests.cleanup_schema
    snapshot = pg.PostgreSQLUsageTests.snapshot
    apply_script = pg.PostgreSQLUsageTests.apply_script

    def setUp(self):
        self.off = patch.dict(os.environ, {'POSTHOG_ENABLED': 'false'}); self.off.start(); self.addCleanup(self.off.stop)
        pg.PostgreSQLUsageTests.setUp(self)
        self.analytics_sql = (Path(__file__).parents[1] / 'migrations/product_analytics.sql').read_text(encoding='utf-8')
        self.apply_script(self.analytics_sql)
        self.env = patch.dict(os.environ, ENV); self.env.start(); self.addCleanup(self.env.stop)

    def test_exact_additive_migration_preserves_existing_data_and_refuses_reapplication(self):
        self.assertEqual(self.snapshot(), self.before)
        self.assertTrue({'analytics_consents', 'analytics_outbox', 'analytics_account_preferences',
                         'analytics_monthly_budget'} <= set(inspect(self.engine).get_table_names()))
        with self.assertRaises(Exception): self.apply_script(self.analytics_sql)
        self.assertEqual(self.snapshot(), self.before)

    def test_concurrent_first_accept_cannot_override_withdrawal_of_same_lease(self):
        barrier = Barrier(2); visitor, revision = uuid4(), uuid4()
        def request(action):
            with self.sessions() as db:
                barrier.wait(timeout=10)
                return analytics.set_consent(db, visitor, revision, action, 1)
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(request, ['accept', 'withdraw']))
        with self.sessions() as db:
            row = db.get(AnalyticsConsent, analytics.visitor_key(visitor))
            self.assertFalse(analytics.eligible(db, row))
            self.assertEqual(db.query(AnalyticsOutbox).count(), 0)

    def test_concurrent_duplicate_batches_store_one_event(self):
        visitor, revision, event_id = uuid4(), uuid4(), uuid4()
        with self.sessions() as db: analytics.set_consent(db, visitor, revision, 'accept')
        barrier = Barrier(8)
        def request(_):
            with self.sessions() as db:
                barrier.wait(timeout=10)
                row = db.query(AnalyticsConsent).filter_by(visitor_key=analytics.visitor_key(visitor)).with_for_update().one()
                accepted = analytics.enqueue(db, row, 'page_viewed', {}, event_id=event_id, source='client', route_key='home')
                db.commit(); return accepted
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(request, range(8)))
        self.assertEqual(sum(results), 1)
        with self.sessions() as db:
            self.assertEqual(db.query(AnalyticsOutbox).count(), 1)
            self.assertEqual(db.query(AnalyticsMonthlyBudget).one().events, 1)

    def test_atomic_monthly_budget_caps_concurrent_visitors(self):
        visitors = [(uuid4(), uuid4()) for _ in range(8)]
        for visitor, revision in visitors:
            with self.sessions() as db: analytics.set_consent(db, visitor, revision, 'accept')
        barrier = Barrier(8)
        def request(pair):
            with self.sessions() as db:
                barrier.wait(timeout=10)
                row = db.get(AnalyticsConsent, analytics.visitor_key(pair[0]))
                accepted = analytics.enqueue(db, row, 'page_viewed', {}, source='client', route_key='home')
                db.commit(); return accepted
        with patch.dict(os.environ, {'POSTHOG_MONTHLY_EVENT_LIMIT': '3'}):
            with ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(request, visitors))
        self.assertEqual(sum(results), 3)
        with self.sessions() as db:
            self.assertEqual(db.query(AnalyticsOutbox).count(), 3)
            self.assertEqual(db.query(AnalyticsMonthlyBudget).one().events, 3)


if __name__ == '__main__': unittest.main()
