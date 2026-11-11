"""Bound Stripe/Lava confirmations share the same primary arbitration."""
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch
from sqlalchemy import event as sql_event
import test_stripe as sf
import test_lava as lf
from database.queries import User, LavaCheckout, BillingEvent, BillingConflict, GPTUsage
from modules import lava_billing as lava

class BillingConflictTests(unittest.TestCase):
    tearDown = sf.StripeTests.tearDown
    send_stripe = sf.StripeTests.send
    send_lava = lf.LavaTests.send
    state = sf.StripeTests.state
    event = lf.LavaTests.event

    def setUp(self):
        sf.StripeTests.setUp(self)
        env = patch.dict('os.environ', lf.ENV); env.start(); self.addCleanup(env.stop)
        self.lava_state = dict(paid=self.start, end=self.end, cancelled=None, terminated=None,
            status='ACTIVE', completed_ids={lf.CONTRACT})
        fetch = patch.object(lava, 'snapshot', side_effect=lambda _: self.lava_state.copy())
        fetch.start(); self.addCleanup(fetch.stop)
        with self.sessions() as db:
            db.add(LavaCheckout(id='lava-bound', user_id=1, contract_id=lf.CONTRACT, offer_id=lf.OFFER,
                product_id=lf.PRODUCT, buyer_email='a@example.test', state='ready', created_at=self.start-timedelta(seconds=1)))
            db.commit()

    def primary(self):
        with self.sessions() as db:
            u = db.get(User, 1)
            return (u.payment_provider, u.provider_subscription_id, u.plan, u.current_period_start,
                u.current_period_end, u.subscription_status, u.scheduled_cancel_at)

    def paid(self, provider):
        return self.send_lava(self.event()) if provider == 'lava' else self.send_stripe()

    def check_order(self, first):
        self.assertEqual(self.paid(first).status_code, 200)
        before = self.primary()
        with self.sessions() as db:
            db.add(GPTUsage(user_id=1, chart_id=1, status='succeeded', plan='premium', period_start=self.start, period_end=self.end)); db.commit()
        second = 'stripe' if first == 'lava' else 'lava'
        body, send = (sf.event(), self.send_stripe) if second == 'stripe' else (self.event(), self.send_lava)
        self.assertEqual(send(body).json()['outcome'], 'billing_conflict')
        self.assertEqual(send(body).json()['outcome'], 'duplicate')
        self.assertEqual(self.primary(), before)
        self.assertEqual(self.state()['gpt_messages_used'], 1)
        with self.sessions() as db:
            case = db.query(BillingConflict).one()
            self.assertEqual((case.provider, case.primary_provider, case.status), (second, first, 'open'))
        return before

    def test_stripe_primary_lava_conflict_and_cancellation(self):
        before = self.check_order('stripe')
        self.lava_state.update(status='CANCELLED', cancelled=datetime.utcnow())
        self.assertEqual(self.send_lava(self.event('subscription.cancelled')).json()['outcome'], 'conflict_lifecycle_recorded')
        self.assertEqual(self.primary(), before)

    def test_lava_primary_stripe_conflict_refund_cancellation_and_resolved_case(self):
        before = self.check_order('lava')
        with self.sessions() as db: db.query(BillingConflict).one().status = 'resolved'; db.commit()
        self.send_stripe(sf.event('refund.updated', 're_test'))
        self.sub.update(status='canceled', ended_at=sf.timestamp(datetime.utcnow()))
        self.send_stripe(sf.event('customer.subscription.deleted', sf.SUB))
        self.assertEqual(self.primary(), before)
        with self.sessions() as db: self.assertEqual(db.query(BillingConflict).one().status, 'resolved')

    def test_unpaid_stripe_cannot_win_primary(self):
        self.sub['status'] = 'incomplete'; self.invoices[sf.INVOICE]['status'] = 'open'
        self.send_stripe(sf.event('invoice.payment_failed'))
        self.assertIsNone(self.primary()[0]); self.paid('lava')
        self.assertEqual(self.primary()[0], 'lava')

    def test_invalid_second_payment_never_creates_conflict(self):
        self.paid('stripe'); body = self.event(); body['amount'] = 1
        self.assertEqual(self.send_lava(body).status_code, 400)
        with self.sessions() as db: self.assertEqual(db.query(BillingConflict).count(), 0)

    def test_conflict_insert_failure_rolls_back_event_for_retry(self):
        self.paid('stripe'); value = self.event()
        def fail(conn, cursor, statement, *args):
            if statement.startswith('INSERT INTO billing_conflicts'): raise RuntimeError('isolated failure')
        sql_event.listen(self.engine, 'before_cursor_execute', fail)
        try: self.assertEqual(self.send_lava(value).status_code, 500)
        finally: sql_event.remove(self.engine, 'before_cursor_execute', fail)
        with self.sessions() as db:
            self.assertEqual(db.query(BillingConflict).count(), 0)
            self.assertEqual(db.query(BillingEvent).filter_by(provider='lava').count(), 0)
        self.assertEqual(self.send_lava(value).json()['outcome'], 'billing_conflict')

    def test_conflict_does_not_normalize_expired_primary(self):
        self.paid('stripe')
        with self.sessions() as db:
            db.get(User, 1).scheduled_cancel_at = datetime.utcnow() - timedelta(seconds=1); db.commit()
        before = self.primary()
        self.assertEqual(self.send_lava(self.event()).json()['outcome'], 'billing_conflict')
        self.assertEqual(self.primary(), before)
