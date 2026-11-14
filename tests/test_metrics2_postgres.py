"""METRICS-2 regression B15: concurrent persist_business with one dedup key -> one outbox row.

Disposable localhost PostgreSQL only (USAGE_TEST_POSTGRES_URL from scripts/test-backend.ps1),
own random schema, dropped after the test. No production data, no network, no real keys.

Interleaving is forced, not hoped for: worker A is held right after its own insert of the
target event (row written, transaction still open) until worker B is observed either waiting
on a PostgreSQL lock or finished. Only then is A released. Every wait is bounded.
"""
import os
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from uuid import uuid4

from sqlalchemy import text

import test_my_charts as fixtures
import test_product_analytics_postgres as analytics_pg
from database.queries import NatalChart, User
from modules import product_analytics as analytics
from modules.product_analytics import AnalyticsOutbox

FLAG = 'LUNARIA_FREE_BETA_ENABLED'
WAIT = 15  # seconds; upper bound for every barrier in this file


@unittest.skipUnless(os.getenv('USAGE_TEST_POSTGRES_URL'), 'Disposable localhost PostgreSQL required')
class Metrics2PostgresTests(unittest.TestCase):
    cleanup_schema = analytics_pg.ProductAnalyticsPostgresTests.cleanup_schema
    snapshot = analytics_pg.ProductAnalyticsPostgresTests.snapshot
    apply_script = analytics_pg.ProductAnalyticsPostgresTests.apply_script
    setUp = analytics_pg.ProductAnalyticsPostgresTests.setUp

    def account_consent(self):
        with self.sessions() as db:
            self.assertTrue(analytics.set_consent(db, uuid4(), uuid4(), 'accept', 1)['accepted'])

    def waiting_on_lock(self):
        with self.admin.connect() as conn:
            return conn.execute(text('SELECT count(*) FROM pg_locks WHERE NOT granted')).scalar() > 0

    def race(self, calls, target):
        """Run two persist_business calls; hold the first inside its open transaction after it enqueues `target`."""
        real_enqueue = analytics.enqueue
        holder = {'thread': None}
        held, release = threading.Event(), threading.Event()

        def enqueue(db, consent, name, *args, **kwargs):
            accepted = real_enqueue(db, consent, name, *args, **kwargs)
            if accepted and name == target and holder['thread'] == threading.get_ident():
                held.set()
                release.wait(WAIT)
            return accepted

        def first():
            holder['thread'] = threading.get_ident()
            analytics.persist_business(*calls[0])

        with patch.object(analytics, 'enqueue', enqueue), ThreadPoolExecutor(max_workers=2) as pool:
            a = pool.submit(first)
            deadline = time.monotonic() + WAIT
            while not (held.is_set() or a.done()):  # on base A finishes: the event name is unknown
                self.assertLess(time.monotonic(), deadline, 'worker A neither held nor finished')
                time.sleep(0.01)
            reached = held.is_set()
            b = pool.submit(analytics.persist_business, *calls[1])
            if reached:
                deadline = time.monotonic() + WAIT
                while not (b.done() or self.waiting_on_lock()):
                    self.assertLess(time.monotonic(), deadline, 'worker B neither blocked nor finished')
                    time.sleep(0.02)
            release.set()
            a.result(WAIT); b.result(WAIT)  # persist_business never raises; this re-raises harness errors
        return reached

    def rows(self, name):
        with self.sessions() as db:
            return [dict(row.properties) for row in db.query(AnalyticsOutbox).filter_by(event_name=name).all()]

    def test_b15_parallel_payment_failure_with_one_key_is_one_row(self):
        self.account_consent()
        webhook = {'interactive': False}
        props = {'provider': 'stripe', 'stage': 'renewal'}
        key = 'payment_failed:stripe:in_isolated_parallel'
        reached = self.race([(self.engine, 1, 'payment_failed', props, key, webhook),
                             (self.engine, 1, 'payment_failed', props, key, webhook)], 'payment_failed')
        rows = self.rows('payment_failed')
        self.assertEqual(len(rows), 1, f'rows={rows} held={reached}')
        self.assertEqual({k: rows[0][k] for k in props}, props)

    def test_b15_parallel_shadow_limit_for_one_account_is_one_row(self):
        with patch.dict(os.environ, {'POSTHOG_ENABLED': 'false'}), self.sessions() as db:
            for chart_id in range(101, 105):  # user 1 already owns chart 1 -> 5 charts, over the free limit of 3
                db.add(NatalChart(id=chart_id, **fixtures.PAYLOAD, user_id=1))  # fixture rows used explicit ids
            db.commit()
            ids = [row.id for row in db.query(NatalChart).filter_by(user_id=1).order_by(NatalChart.id.desc()).limit(2)]
            self.assertEqual(db.get(User, 1).plan, 'free')
        self.account_consent()
        webhook = {'interactive': False}
        with patch.dict(os.environ, {FLAG: 'true'}):
            reached = self.race([(self.engine, 1, 'chart_creation_completed', {'actor': 'account'}, f'chart:{ids[0]}', webhook),
                                 (self.engine, 1, 'chart_creation_completed', {'actor': 'account'}, f'chart:{ids[1]}', webhook)],
                                'free_limit_would_block')
        self.assertEqual(len(self.rows('chart_creation_completed')), 2)
        rows = self.rows('free_limit_would_block')
        self.assertEqual(len(rows), 1, f'rows={rows} held={reached}')
        self.assertEqual(rows[0]['resource'], 'chart')


if __name__ == '__main__':
    unittest.main()
