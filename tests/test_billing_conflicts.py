"""Both provider orders, signed endpoints and manual conflict isolation. No live API."""
import copy
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace as NS
from unittest.mock import patch, Mock
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import event as sql_event
import test_lava as lava_tests
import test_paddle as paddle_tests
from database.queries import User, LavaCheckout, PaddleCheckout, PaddleEvent, BillingConflict, GPTUsage
from modules import lava_billing as lava, paddle_billing as paddle


class BillingConflictTests(unittest.TestCase):
    tearDown = lava_tests.LavaTests.tearDown
    provider = lava_tests.LavaTests.provider
    event = lava_tests.LavaTests.event
    financial = lava_tests.LavaTests.financial
    send = lava_tests.LavaTests.send
    state = lava_tests.LavaTests.state
    payload = paddle_tests.PaddleTests.payload
    send_paddle = paddle_tests.PaddleTests.send

    def setUp(self):
        lava_tests.LavaTests.setUp(self)
        self.binding = str(uuid4())
        self.env = patch.dict('os.environ', {'PADDLE_PREMIUM_PRICE_ID': paddle_tests.PRICE,
            'PADDLE_WEBHOOK_SECRET': paddle_tests.SECRET})
        self.env.start(); self.addCleanup(self.env.stop)
        confirm = patch.object(paddle, 'confirm_initial_payment')
        self.confirm = confirm.start(); self.addCleanup(confirm.stop)
        with self.sessions() as db:
            db.get(LavaCheckout, 'bound').payment_url = 'https://pay.example.test/existing'
            db.add(PaddleCheckout(id=self.binding, user_id=1, transaction_id=paddle_tests.TRANSACTION, state='ready'))
            db.commit()

    def primary(self):
        with self.sessions() as db:
            u = db.get(User, 1)
            return (u.payment_provider, u.provider_subscription_id, u.plan,
                    u.current_period_start, u.current_period_end, u.subscription_status, u.scheduled_cancel_at)

    def paid(self, provider):
        return self.send(self.event()) if provider == 'lava' else self.send_paddle(self.payload())

    def test_lava_pending_to_paddle_success_preserves_lava_binding(self):
        with patch.object(paddle, 'paddle_client') as sdk:
            r = self.client.post('/payments/paddle/checkout', headers=self.owner)
            self.assertEqual(r.status_code, 200, r.text)
            sdk.return_value.transactions.create.assert_not_called()
        self.assertEqual(self.paid('paddle').status_code, 200)
        self.assertEqual(self.primary()[0], 'paddle')
        with self.sessions() as db:
            self.assertEqual(db.get(LavaCheckout, 'bound').contract_id, lava_tests.CONTRACT)
            self.assertEqual(db.get(LavaCheckout, 'bound').state, 'ready')

    def test_paddle_pending_to_lava_success_preserves_paddle_binding(self):
        r = self.client.post('/payments/lava/checkout', headers=self.owner)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.paid('lava').status_code, 200)
        self.assertEqual(self.primary()[0], 'lava')
        with self.sessions() as db:
            self.assertEqual(db.get(PaddleCheckout, self.binding).transaction_id, paddle_tests.TRANSACTION)
            self.assertEqual(db.get(PaddleCheckout, self.binding).state, 'ready')

    def check_order(self, first):
        self.assertEqual(self.paid(first).status_code, 200)
        before = self.primary()
        with self.sessions() as db:
            db.add(GPTUsage(user_id=1, chart_id=123, status='succeeded', plan='premium',
                period_start=self.start, period_end=self.end)); db.commit()
        second = 'paddle' if first == 'lava' else 'lava'
        body = self.payload() if second == 'paddle' else self.event()
        send = self.send_paddle if second == 'paddle' else self.send
        r = send(body)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()['outcome'], 'billing_conflict')
        self.assertEqual(send(body).json()['outcome'], 'duplicate')
        # A different event ID/time for the same subscription is still one case.
        again = copy.deepcopy(body)
        if second == 'paddle': again['event_id'] = 'evt_' + uuid4().hex[:26]
        else: again['timestamp'] = lava_tests.iso(datetime.utcnow())
        self.assertEqual(send(again).json()['outcome'], 'billing_conflict')
        self.assertEqual(self.primary(), before)
        usage = self.state()
        self.assertEqual((usage['gpt_messages_used'], usage['gpt_messages_limit']), (1, 300))
        with self.sessions() as db:
            case = db.query(BillingConflict).one()
            self.assertEqual((case.provider, case.primary_provider, case.status), (second, first, 'open'))
            self.assertTrue(case.subscription_id and case.checkout_id and case.payment_id and case.first_event_id)
        return before

    def test_paddle_then_lava_conflict_duplicates_and_financial_events(self):
        before = self.check_order('paddle')
        for body in (self.financial(canceled=True), self.financial('chargeback.initiated', canceled=True),
                     self.event('subscription.cancelled')):
            r = self.send(body)
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(r.json()['outcome'], 'conflict_lifecycle_recorded')
        self.assertEqual(self.primary(), before)

    def test_lava_then_paddle_conflict_and_cancellation(self):
        before = self.check_order('lava')
        r = self.send_paddle(self.payload('canceled', current_billing_period=None))
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()['outcome'], 'conflict_lifecycle_recorded')
        self.assertEqual(self.primary(), before)

    def test_unpaid_paddle_cannot_win_primary_slot(self):
        self.assertEqual(self.send_paddle(self.payload('trialing')).status_code, 200)
        self.assertIsNone(self.primary()[0])
        self.assertEqual(self.paid('lava').status_code, 200)
        self.assertEqual(self.primary()[0], 'lava')

    def test_unpaid_lava_cancellation_cannot_win_primary_slot(self):
        self.snapshot.update(status='FAILED',subscriptionStatus='CANCELLED',terminatedAt=lava_tests.iso(datetime.utcnow()))
        self.assertEqual(self.send(self.event('subscription.cancelled')).status_code, 200)
        self.assertIsNone(self.primary()[0])
        self.assertEqual(self.paid('paddle').status_code, 200)
        self.assertEqual(self.primary()[0], 'paddle')

    def test_conflict_insert_failure_rolls_back_event_for_retry(self):
        self.paid('paddle')
        value = self.event()
        def fail(conn, cursor, statement, *args):
            if statement.startswith('INSERT INTO billing_conflicts'): raise RuntimeError('test failure')
        sql_event.listen(self.engine, 'before_cursor_execute', fail)
        try: self.assertEqual(self.send(value).status_code, 500)
        finally: sql_event.remove(self.engine, 'before_cursor_execute', fail)
        with self.sessions() as db:
            self.assertEqual(db.query(BillingConflict).count(), 0)
            self.assertEqual(db.query(PaddleEvent).filter_by(provider='lava').count(), 0)
        self.assertEqual(self.send(value).json()['outcome'], 'billing_conflict')

    def test_invalid_second_payment_never_creates_conflict(self):
        self.paid('paddle')
        body = self.event(); body['amount'] = 10
        self.assertEqual(self.send(body).status_code, 400)
        self.snapshot['status'] = 'IN_PROGRESS'
        self.assertEqual(self.send(self.event()).status_code, 503)
        with self.sessions() as db: self.assertEqual(db.query(BillingConflict).count(), 0)

    def test_conflict_does_not_normalize_expired_primary_period(self):
        self.paid('paddle')
        with self.sessions() as db:
            db.get(User, 1).scheduled_cancel_at = datetime.utcnow() - timedelta(seconds=1)
            db.commit()
        before = self.primary()
        self.assertEqual(self.send(self.event()).json()['outcome'], 'billing_conflict')
        self.assertEqual(self.primary(), before)

    def test_second_lava_subscription_same_provider_is_conflict(self):
        self.paid('lava'); before = self.primary()
        other = str(uuid4())
        with self.sessions() as db:
            db.add(LavaCheckout(user_id=1, contract_id=other, offer_id=lava_tests.OFFER,
                product_id=lava_tests.PRODUCT, buyer_email='a@example.test', state='ready')); db.commit()
        self.snapshot['id'] = other
        body = self.event(); body['contractId'] = other
        self.assertEqual(self.send(body).json()['outcome'], 'billing_conflict')
        self.assertEqual(self.primary(), before)


class PaddlePaymentConfirmationTests(unittest.TestCase):
    def test_completed_transaction_and_binding_are_required(self):
        transaction = NS(id=paddle_tests.TRANSACTION, subscription_id=paddle_tests.SUB,
            customer_id=paddle_tests.CUSTOMER, status=NS(value='completed'),
            custom_data=NS(data={'user_id':'1','checkout_binding':'binding'}),
            items=[NS(quantity=1, price=NS(id=paddle_tests.PRICE))])
        sdk = Mock(); sdk.transactions.get.return_value = transaction
        with patch.dict('os.environ', {'PADDLE_PREMIUM_PRICE_ID':paddle_tests.PRICE}), patch.object(paddle,'paddle_client',return_value=sdk):
            args = (paddle_tests.TRANSACTION,paddle_tests.SUB,paddle_tests.CUSTOMER,'binding',1)
            paddle.confirm_initial_payment(*args)
            transaction.status.value = 'billed'
            with self.assertRaises(HTTPException): paddle.confirm_initial_payment(*args)
            transaction.status.value = 'completed'; transaction.customer_id = 'foreign'
            with self.assertRaises(ValueError): paddle.confirm_initial_payment(*args)
            transaction.customer_id = paddle_tests.CUSTOMER; transaction.items[0].price.id = 'wrong'
            with self.assertRaises(ValueError): paddle.confirm_initial_payment(*args)
            sdk.transactions.create.assert_not_called()
