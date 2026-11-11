"""Disposable PostgreSQL: real row locks, retries and metadata migration."""
import os
import copy
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from threading import Barrier, Event, local
from unittest.mock import patch
from fastapi import HTTPException
from sqlalchemy import inspect
import test_postgres_usage as pg
import test_lava as lf
import test_stripe as sf
from database.queries import User, LavaCheckout, StripeCheckout, BillingCustomer, BillingPeriod, BillingEvent, BillingConflict, GPTUsage
from modules import lava_billing as lava, stripe_billing as stripe

@unittest.skipUnless(os.getenv('USAGE_TEST_POSTGRES_URL'), 'Local PostgreSQL test URL not configured')
class BillingConflictPostgresTests(unittest.TestCase):
    snapshot = pg.PostgreSQLUsageTests.snapshot
    cleanup_schema = pg.PostgreSQLUsageTests.cleanup_schema
    apply_script = pg.PostgreSQLUsageTests.apply_script

    def setUp(self):
        pg.PostgreSQLUsageTests.setUp(self)
        self.start = datetime.utcnow().replace(microsecond=0) - timedelta(days=1)
        self.end = self.start + timedelta(days=30)
        self.sub = sf.subscription(self.start, self.end)
        self.invoices = {sf.INVOICE: sf.invoice(self.start, self.end)}
        self.session = {}
        env = patch.dict(os.environ, sf.ENV); env.start(); self.addCleanup(env.stop)
        sf.install_fake(self)
        with self.sessions() as db:
            db.add(LavaCheckout(id='lava', user_id=1, contract_id=lf.CONTRACT,
                offer_id=lf.OFFER, product_id=lf.PRODUCT, buyer_email='test@example.com', state='ready'))
            db.add(StripeCheckout(id='bound', user_id=1, account_id=sf.ACCOUNT, livemode=True,
                customer_id=sf.CUSTOMER, session_id=sf.SESSION, subscription_id=sf.SUB, state='ready'))
            db.add(BillingCustomer(id='customer', user_id=1, provider='stripe', account_id=sf.ACCOUNT, livemode=True, customer_id=sf.CUSTOMER))
            db.commit()
        self.stripe_event = sf.event()
        self.lava_event = {'eventType':'payment.success','contractId':lf.CONTRACT,
            'timestamp':lf.iso(self.start),'buyer':{'email':'test@example.com'},'product':{'id':lf.PRODUCT},
            'amount':799,'currency':'RUB','status':'subscription-active'}
        self.state = dict(paid=self.start,end=self.end,cancelled=None,terminated=None,
            status='ACTIVE',completed_ids={lf.CONTRACT})

    def run_provider(self, provider, event=None):
        with self.sessions() as db:
            return stripe.process_event(db, event or self.stripe_event) if provider == 'stripe' else lava.process_event(db, self.lava_event)

    def race(self, providers):
        barrier = Barrier(2)
        def stripe_read(_):
            barrier.wait(timeout=10)
            return copy.deepcopy(self.sub)
        def lava_read(_):
            barrier.wait(timeout=10)
            return self.state.copy()
        with patch.object(self.api.subscriptions, 'retrieve', side_effect=stripe_read), patch.object(lava,'snapshot',side_effect=lava_read), ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(self.run_provider, p) for p in providers]
            return [f.result(timeout=20) for f in futures]

    def test_simultaneous_success_one_primary_one_conflict(self):
        with self.sessions() as db: before = db.query(GPTUsage).count()
        result = self.race(['lava','stripe'])
        self.assertIn('billing_conflict', result)
        self.assertTrue('granted' in result or 'processed' in result)
        with self.sessions() as db:
            user = db.get(User,1); case = db.query(BillingConflict).one()
            self.assertEqual(user.plan,'premium')
            self.assertEqual(user.current_period_start,self.start)
            self.assertEqual(user.current_period_end,self.end)
            self.assertEqual(case.primary_provider,user.payment_provider)
            self.assertNotEqual(case.provider,user.payment_provider)
            self.assertEqual(db.query(BillingEvent).count(),2)
            self.assertEqual(db.query(GPTUsage).count(),before)

    def test_duplicate_lava_commits_once(self):
        self.assertEqual(sorted(self.race(['lava','lava'])), ['duplicate','processed'])

    def test_duplicate_stripe_commits_once(self):
        self.assertEqual(sorted(self.race(['stripe','stripe'])), ['duplicate','granted'])
        with self.sessions() as db:
            self.assertEqual(db.query(BillingPeriod).count(),1)
            self.assertEqual(db.query(BillingEvent).count(),1)

    def test_concurrent_conflict_delivery_single_case(self):
        self.run_provider('stripe')
        self.assertEqual(sorted(self.race(['lava','lava'])), ['billing_conflict','duplicate'])
        with self.sessions() as db:
            self.assertEqual(db.query(BillingConflict).count(),1)
            self.assertEqual(db.get(User,1).payment_provider,'stripe')

    def test_distinct_events_race_retries_stale_revision_without_double_quota(self):
        barrier = Barrier(2)
        def read(_):
            barrier.wait(timeout=10); return copy.deepcopy(self.sub)
        events = [sf.event(), sf.event('customer.subscription.updated', sf.SUB)]
        def run(value):
            try: return self.run_provider('stripe', value)
            except HTTPException as exc: return exc.status_code
        with patch.object(self.api.subscriptions, 'retrieve', side_effect=read), ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(run, events))
        self.assertEqual(sorted(map(str,results)), ['503','granted'])
        retry = events[results.index(503)]
        self.assertEqual(self.run_provider('stripe',retry),'recorded')
        with self.sessions() as db:
            self.assertEqual(db.query(BillingPeriod).count(),1)
            self.assertEqual(db.query(BillingEvent).count(),2)

    def test_concurrent_stale_active_cannot_undo_cancellation(self):
        self.run_provider('stripe')
        ready, committed = Event(), Event()
        original = copy.deepcopy(self.sub)
        def read(_):
            ready.set()
            if not committed.wait(10): raise RuntimeError('timeout')
            return original
        stale = sf.event()
        def old_worker():
            try: return self.run_provider('stripe',stale)
            except HTTPException as exc: return exc.status_code
        with ThreadPoolExecutor(max_workers=1) as pool:
            with patch.object(self.api.subscriptions, 'retrieve', side_effect=read):
                future = pool.submit(old_worker)
                self.assertTrue(ready.wait(10))
                self.sub.update(status='canceled',ended_at=sf.timestamp(datetime.utcnow()))
                # Separate client read for the concurrently committed cancellation.
                with patch.object(self.api.subscriptions, 'retrieve', side_effect=lambda _: copy.deepcopy(self.sub)):
                    self.run_provider('stripe', sf.event('customer.subscription.deleted',sf.SUB))
                committed.set()
                self.assertEqual(future.result(timeout=10),503)
        self.run_provider('stripe',stale)
        with self.sessions() as db: self.assertEqual(db.get(User,1).plan,'free')

    def test_simultaneous_checkout_uses_one_binding_and_remote_idempotency_key(self):
        barrier = Barrier(2)
        def customer(params, options):
            barrier.wait(timeout=10)
            return {'id':'cus_other','livemode':True}
        self.api.customers.create.side_effect = customer
        def run(_):
            with self.sessions() as db: return stripe.create_checkout(db,2)
        with ThreadPoolExecutor(max_workers=2) as pool: results = list(pool.map(run,range(2)))
        self.assertEqual(results[0],results[1])
        keys = [c.kwargs['options']['idempotency_key'] for c in self.api.checkout.sessions.create.call_args_list]
        self.assertEqual(len(set(keys)),1)
        with self.sessions() as db:
            self.assertEqual(db.query(StripeCheckout).filter_by(user_id=2).count(),1)
            self.assertEqual(db.query(BillingCustomer).filter_by(user_id=2).count(),1)


@unittest.skipUnless(os.getenv('USAGE_TEST_POSTGRES_URL'), 'Local PostgreSQL test URL not configured')
class StripeMigrationTests(unittest.TestCase):
    snapshot = pg.PostgreSQLUsageTests.snapshot
    cleanup_schema = pg.PostgreSQLUsageTests.cleanup_schema
    def apply_script(self,script):
        if 'RENAME TO billing_events' not in script: pg.PostgreSQLUsageTests.apply_script(self,script)
    def setUp(self):
        pg.PostgreSQLUsageTests.setUp(self)
        with self.engine.begin() as c:
            c.exec_driver_sql("INSERT INTO paddle_events (provider,event_id,event_type,occurred_at,processed_at,outcome) VALUES ('lava','lava:preserved','payment.success',now(),now(),'processed')")
            c.exec_driver_sql("UPDATE users SET plan='premium',payment_provider='lava',provider_subscription_id='preserved',current_period_start=now(),current_period_end=now()+interval '1 month' WHERE id=1")
            c.exec_driver_sql("INSERT INTO lava_checkouts (id,user_id,contract_id,offer_id,product_id,buyer_email,state,created_at) VALUES ('history',1,'preserved','offer','product','test@example.test','completed',now())")
            self.rows = {name:c.exec_driver_sql('SELECT * FROM '+name+' ORDER BY 1').all() for name in ('users','lava_checkouts','gpt_usage','paddle_events','paddle_checkouts')}
    def verify_rows(self):
        with self.engine.connect() as c:
            for name,rows in self.rows.items():
                target = 'billing_events' if name == 'paddle_events' else name
                self.assertEqual(c.exec_driver_sql('SELECT * FROM '+target+' ORDER BY 1').all(),rows)
    def migrate(self,script=None): pg.PostgreSQLUsageTests.apply_script(self,script or self.stripe_script)
    def test_rename_preserves_every_lava_user_quota_event_and_legacy_row(self):
        self.migrate(); self.verify_rows()
        with self.engine.connect() as c:
            names = inspect(c).get_table_names()
            self.assertNotIn('paddle_events',names)
            self.assertTrue({'billing_events','billing_periods','billing_customers','stripe_checkouts'} <= set(names))
        with self.assertRaises(Exception): self.migrate()
        self.verify_rows()
    def test_failure_rolls_back_rename_and_tables_atomically(self):
        with self.assertRaises(Exception): self.migrate(self.stripe_script.replace('COMMIT;', 'SELECT 1/0; COMMIT;'))
        with self.engine.connect() as c:
            self.assertIn('paddle_events',inspect(c).get_table_names())
            self.assertNotIn('billing_events',inspect(c).get_table_names())
            self.assertNotIn('stripe_checkouts',inspect(c).get_table_names())
        self.migrate(); self.verify_rows()
