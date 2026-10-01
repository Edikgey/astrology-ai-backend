"""Local disposable PostgreSQL: cross-provider arbitration and migration safety."""
import os
import unittest
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from threading import Barrier
from unittest.mock import patch
from uuid import uuid4

from sqlalchemy import inspect
import test_postgres_usage as pg
import test_lava as lf
import test_paddle as pf
from database.queries import User, LavaCheckout, PaddleCheckout, PaddleEvent, BillingConflict, GPTUsage
from modules import lava_billing as lava, paddle_billing as paddle


@unittest.skipUnless(os.getenv('USAGE_TEST_POSTGRES_URL'), 'Local PostgreSQL test URL not configured')
class BillingConflictPostgresTests(unittest.TestCase):
    snapshot = pg.PostgreSQLUsageTests.snapshot
    cleanup_schema = pg.PostgreSQLUsageTests.cleanup_schema
    apply_script = pg.PostgreSQLUsageTests.apply_script

    def setUp(self):
        pg.PostgreSQLUsageTests.setUp(self)
        self.start = datetime.utcnow() - timedelta(days=1)
        self.end = self.start + timedelta(days=30)
        self.binding = str(uuid4())
        with self.sessions() as db:
            db.add(LavaCheckout(id='lava', user_id=1, contract_id=lf.CONTRACT,
                offer_id=lf.OFFER, product_id=lf.PRODUCT, buyer_email='test@example.com', state='ready'))
            db.add(PaddleCheckout(id=self.binding, user_id=1, transaction_id=pf.TRANSACTION, state='ready'))
            db.commit()
        self.paddle_event = pf.PaddleTests.payload(self)
        self.lava_event = {'eventType':'payment.success','contractId':lf.CONTRACT,
            'timestamp':lf.iso(self.start),'buyer':{'email':'test@example.com'},'product':{'id':lf.PRODUCT},
            'amount':799,'currency':'RUB','status':'subscription-active'}
        self.state = dict(paid=self.start,end=self.end,cancelled=None,terminated=None,
                          status='ACTIVE',completed_ids={lf.CONTRACT})

    def race(self, providers):
        barrier = Barrier(2)
        def confirmed(*args):
            barrier.wait(timeout=10)
            return self.state.copy()
        def run(provider):
            with self.sessions() as db:
                if provider == 'paddle': return paddle.process_event(db,self.paddle_event)
                return lava.process_event(db,self.lava_event)
        with patch.dict(os.environ,{'PADDLE_PREMIUM_PRICE_ID':pf.PRICE}), \
                patch.object(paddle,'confirm_initial_payment',side_effect=confirmed), \
                patch.object(lava,'snapshot',side_effect=confirmed), ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(run, p) for p in providers]
            return [f.result(timeout=20) for f in futures]

    def test_simultaneous_success_has_one_primary_one_conflict_and_unchanged_usage(self):
        with self.sessions() as db: before = db.query(GPTUsage).count()
        self.assertEqual(sorted(self.race(['lava','paddle'])), ['billing_conflict','processed'])
        with self.sessions() as db:
            user = db.get(User,1); case = db.query(BillingConflict).one()
            self.assertEqual(user.plan,'premium')
            self.assertEqual(user.current_period_start,self.start)
            self.assertEqual(user.current_period_end,self.end)
            self.assertEqual(case.primary_provider,user.payment_provider)
            self.assertNotEqual(case.provider,user.payment_provider)
            self.assertEqual(db.query(PaddleEvent).count(),2)
            self.assertEqual(db.query(GPTUsage).count(),before)

    def test_duplicate_lava_success_commits_once(self):
        self.assertEqual(sorted(self.race(['lava','lava'])),['duplicate','processed'])
        with self.sessions() as db:
            self.assertEqual(db.query(BillingConflict).count(),0)
            self.assertEqual(db.query(PaddleEvent).count(),1)

    def test_duplicate_paddle_success_commits_once(self):
        self.assertEqual(sorted(self.race(['paddle','paddle'])),['duplicate','processed'])
        with self.sessions() as db:
            self.assertEqual(db.query(BillingConflict).count(),0)
            self.assertEqual(db.query(PaddleEvent).count(),1)

    def test_concurrent_conflict_delivery_records_single_case(self):
        with patch.dict(os.environ,{'PADDLE_PREMIUM_PRICE_ID':pf.PRICE}), patch.object(paddle,'confirm_initial_payment'):
            with self.sessions() as db: paddle.process_event(db,self.paddle_event)
        self.assertEqual(sorted(self.race(['lava','lava'])),['billing_conflict','duplicate'])
        with self.sessions() as db:
            self.assertEqual(db.query(BillingConflict).count(),1)
            self.assertEqual(db.get(User,1).payment_provider,'paddle')

    def test_migration_additive_constraints_and_repeat_fails_without_data_loss(self):
        self.assertEqual(self.snapshot(),self.before)
        with self.engine.connect() as c:
            names = {x['name'] for x in inspect(c).get_unique_constraints('billing_conflicts')}
            self.assertIn('uq_billing_conflict_subscription',names)
        script = (Path(__file__).parents[1]/'migrations/billing_conflicts.sql').read_text()
        with self.assertRaises(Exception): self.apply_script(script)
        self.assertEqual(self.snapshot(),self.before)

    def test_failed_migration_rolls_back_new_table(self):
        with self.engine.begin() as c: c.exec_driver_sql('DROP TABLE billing_conflicts')
        script = (Path(__file__).parents[1]/'migrations/billing_conflicts.sql').read_text()
        with self.assertRaises(Exception): self.apply_script(script.replace('COMMIT;', 'SELECT 1/0; COMMIT;'))
        with self.engine.connect() as c: self.assertNotIn('billing_conflicts',inspect(c).get_table_names())
        self.assertEqual(self.snapshot(),self.before)
