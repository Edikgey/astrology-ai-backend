"""Isolated Stripe API contracts and real SDK signature verification. No network."""
import copy
import hashlib
import hmac
import json
import os
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch
from uuid import uuid4

from fastapi import HTTPException
import test_my_charts as fixtures
from database.queries import User, StripeCheckout, BillingCustomer, BillingEvent, BillingPeriod, BillingConflict, GPTUsage, LavaCheckout
from modules import stripe_billing as billing, usage, lava_billing

ACCOUNT, CUSTOMER, SUB, PRICE = 'acct_isolated', 'cus_isolated', 'sub_isolated', 'price_isolated'
SESSION, INVOICE, SECRET = 'cs_live_isolated', 'in_isolated', 'isolated-signing-secret'
ENV = {'STRIPE_SECRET_KEY': 'sk_live_isolated_not_a_credential', 'STRIPE_ACCOUNT_ID': ACCOUNT,
    'STRIPE_LIVEMODE': 'true', 'STRIPE_PREMIUM_PRICE_ID': PRICE, 'STRIPE_WEBHOOK_SECRET': SECRET,
    'STRIPE_API_VERSION': billing.API_VERSION, 'STRIPE_CHECKOUT_ENABLED': 'true',
    'STRIPE_PORTAL_CONFIGURATION_ID': 'bpc_isolated',
    'STRIPE_CHECKOUT_RETURN_URL': 'https://mylunariaai.com/my-charts?billing=stripe&session_id={CHECKOUT_SESSION_ID}',
    'STRIPE_PORTAL_RETURN_URL': 'https://mylunariaai.com/my-charts?billing=stripe'}


def timestamp(value): return int(value.replace(tzinfo=timezone.utc).timestamp())


def price():
    return {'id': PRICE, 'livemode': True, 'active': True, 'currency': 'usd', 'unit_amount': 999,
        'recurring': {'interval': 'month', 'interval_count': 1, 'usage_type': 'licensed'}}


def invoice(start, end, invoice_id=INVOICE, subscription=SUB):
    return {'id': invoice_id, 'livemode': True, 'parent': {'type': 'subscription_details', 'subscription_details': {'subscription': subscription}},
        'customer': CUSTOMER, 'status': 'paid', 'amount_paid': 999, 'currency': 'usd', 'billing_reason': 'subscription_cycle',
        'lines': {'has_more': False, 'data': [{'quantity': 1, 'pricing': {'price_details': {'price': PRICE}},
            'parent': {'subscription_item_details': {'proration': False}}, 'period': {'start': timestamp(start), 'end': timestamp(end)}}]}}


def subscription(start, end, binding='bound', user_id=1, subscription_id=SUB):
    return {'id': subscription_id, 'livemode': True, 'customer': CUSTOMER, 'status': 'active',
        'metadata': {'checkout_binding': binding, 'user_id': str(user_id)}, 'latest_invoice': INVOICE,
        'cancel_at': None, 'cancel_at_period_end': False, 'ended_at': None,
        'items': {'has_more': False, 'data': [{'quantity': 1, 'price': price(), 'current_period_end': timestamp(end)}]}}


def event(kind='invoice.paid', object_id=INVOICE, event_id=None):
    return {'id': event_id or 'evt_' + uuid4().hex, 'livemode': True, 'api_version': billing.API_VERSION,
        'created': int(time.time()), 'type': kind, 'data': {'object': {'id': object_id}}}


def install_fake(test):
    test.api = Mock()
    test.api.prices.retrieve.side_effect = lambda _: price()
    test.api.subscriptions.retrieve.side_effect = lambda _: copy.deepcopy(test.sub)
    test.api.invoices.retrieve.side_effect = lambda i: copy.deepcopy(test.invoices[i])
    test.api.invoice_payments.list.side_effect = lambda p: {'has_more': False, 'data': [{
        'id': 'inpay_test', 'invoice': p.get('invoice', INVOICE), 'amount_paid': 999,
        'payment': {'type': 'payment_intent', 'payment_intent': 'pi_isolated'}}]}
    test.api.payment_intents.retrieve.return_value = {'id': 'pi_isolated', 'livemode': True, 'status': 'succeeded', 'currency': 'usd', 'customer': CUSTOMER}
    test.api.checkout.sessions.retrieve.side_effect = lambda _: copy.deepcopy(test.session)
    test.api.customers.create.return_value = {'id': CUSTOMER + '2', 'livemode': True}
    test.api.checkout.sessions.create.return_value = {'id': 'cs_live_new', 'livemode': True, 'status': 'open',
        'client_secret': 'cs_live_new_secret_test', 'expires_at': int(time.time()) + 3600}
    test.api.billing_portal.sessions.create.return_value = {'url': 'https://billing.stripe.com/p/session/test'}
    test.api.refunds.retrieve.return_value = {'id': 're_test', 'livemode': True, 'charge': 'ch_test', 'amount': 999, 'currency': 'usd', 'status': 'succeeded'}
    test.api.charges.retrieve.return_value = {'id': 'ch_test', 'livemode': True, 'payment_intent': 'pi_isolated', 'amount_refunded': 999, 'currency': 'usd'}
    test.api.disputes.retrieve.return_value = {'id': 'dp_test', 'livemode': True, 'charge': 'ch_test', 'amount': 999, 'currency': 'usd', 'status': 'needs_response'}
    mock = patch.object(billing, 'api', return_value=test.api); mock.start(); test.addCleanup(mock.stop)


class StripeTests(unittest.TestCase):
    tearDown = fixtures.MyChartsTests.tearDown

    def setUp(self):
        fixtures.MyChartsTests.setUp(self)
        env = patch.dict(os.environ, ENV); env.start(); self.addCleanup(env.stop)
        self.start = datetime.utcnow().replace(microsecond=0) - timedelta(days=2)
        self.end = self.start + timedelta(days=31)
        self.sub = subscription(self.start, self.end)
        self.invoices = {INVOICE: invoice(self.start, self.end)}
        self.session = {'id': SESSION, 'livemode': True, 'status': 'open', 'mode': 'subscription', 'customer': CUSTOMER,
            'subscription': SUB, 'metadata': {'checkout_binding': 'bound', 'user_id': '1'},
            'client_secret': SESSION + '_secret_test', 'expires_at': int(time.time()) + 3600}
        with self.sessions() as db:
            db.add(BillingCustomer(id='customer-binding', user_id=1, provider='stripe', account_id=ACCOUNT, livemode=True, customer_id=CUSTOMER))
            db.add(StripeCheckout(id='bound', user_id=1, account_id=ACCOUNT, livemode=True, customer_id=CUSTOMER,
                session_id=SESSION, subscription_id=SUB, state='ready'))
            db.commit()
        install_fake(self)

    def send(self, value=None, signature=None):
        body = json.dumps(value or event()).encode()
        stamp = str(int(time.time()))
        sig = hmac.new(SECRET.encode(), stamp.encode() + b'.' + body, hashlib.sha256).hexdigest()
        return self.client.post('/payments/stripe/webhook', content=body,
            headers={'Stripe-Signature': signature or f't={stamp},v1={sig}'})

    def state(self): return self.client.get('/account/usage', headers=self.owner).json()

    def test_activation_allocation_and_duplicate(self):
        value = event()
        self.assertEqual(self.send(value).status_code, 200)
        state = self.state()
        self.assertEqual((state['plan'], state['gpt_messages_limit'], state['saved_charts_limit']), ('premium', 300, 10))
        with self.sessions() as db:
            db.add(GPTUsage(user_id=1, chart_id=1, status='succeeded', plan='premium', period_start=self.start, period_end=self.end)); db.commit()
        self.assertEqual(self.send(value).json()['outcome'], 'duplicate')
        self.assertEqual(self.state()['gpt_messages_used'], 1)
        with self.sessions() as db:
            self.assertEqual(db.query(BillingPeriod).count(), 1)
            self.assertEqual(db.query(BillingEvent).count(), 1)

    def test_distinct_events_same_invoice_allocate_once(self):
        self.send()
        self.send(event('checkout.session.completed', SESSION))
        self.send(event('customer.subscription.updated', SUB))
        with self.sessions() as db:
            self.assertEqual(db.query(BillingPeriod).count(), 1)
            self.assertEqual(db.get(StripeCheckout, 'bound').state, 'completed')

    def test_renewal_once_and_old_invoice_cannot_revert_period(self):
        self.send()
        new_start, new_end = self.end, self.end + timedelta(days=30)
        self.invoices['in_renewal'] = invoice(new_start, new_end, 'in_renewal')
        self.sub['latest_invoice'] = 'in_renewal'
        self.sub['items']['data'][0]['current_period_end'] = timestamp(new_end)
        with patch.object(billing, 'datetime') as clock:
            clock.utcnow.return_value = new_start + timedelta(hours=1)
            clock.utcfromtimestamp.side_effect = datetime.utcfromtimestamp
            value = event(object_id='in_renewal')
            self.assertEqual(self.send(value).status_code, 200)
            self.assertEqual(self.send(value).json()['outcome'], 'duplicate')
            self.assertEqual(self.send(event()).status_code, 200)
        with self.sessions() as db:
            user = db.get(User, 1)
            self.assertEqual(user.current_period_start, new_start)
            self.assertEqual(db.query(BillingPeriod).count(), 2)

    def test_first_failure_and_failed_renewal_never_allocate(self):
        self.invoices[INVOICE]['status'] = 'open'; self.sub['status'] = 'incomplete'
        self.send(event('invoice.payment_failed'))
        self.assertEqual(self.state()['plan'], 'free')
        with self.sessions() as db: self.assertIsNone(db.get(User, 1).payment_provider)
        self.invoices[INVOICE]['status'] = 'paid'; self.sub['status'] = 'active'; self.send()
        self.invoices['in_failed'] = invoice(self.end, self.end + timedelta(days=30), 'in_failed')
        self.invoices['in_failed']['status'] = 'open'; self.sub['status'] = 'past_due'
        self.send(event('invoice.payment_failed', 'in_failed'))
        with self.sessions() as db:
            self.assertEqual(db.get(User, 1).current_period_end, self.end)
            self.assertEqual(db.query(BillingPeriod).count(), 1)

    def test_scheduled_cancel_and_resumption_preserve_quota(self):
        self.send()
        self.sub['cancel_at'] = timestamp(self.end)
        self.send(event('customer.subscription.updated', SUB))
        self.assertTrue(self.state()['cancel_at_period_end'])
        self.sub['cancel_at'] = None
        self.send(event('customer.subscription.updated', SUB))
        self.assertFalse(self.state()['cancel_at_period_end'])
        with self.sessions() as db: self.assertEqual(db.query(BillingPeriod).count(), 1)

    def test_terminal_cancel_and_delayed_paid_invoice_do_not_resurrect(self):
        self.send(); self.sub.update(status='canceled', ended_at=int(time.time()))
        self.send(event('customer.subscription.deleted', SUB))
        self.assertEqual(self.state()['plan'], 'free')
        self.send(event())
        self.assertEqual(self.state()['plan'], 'free')
        self.sub.update(status='active', ended_at=None)
        self.assertEqual(self.send(event()).status_code, 503)

    def test_full_partial_pending_failed_refunds_are_audit_only(self):
        self.send()
        for status, amount in [('pending', 999), ('succeeded', 100), ('succeeded', 999), ('failed', 999)]:
            self.api.refunds.retrieve.return_value.update(status=status, amount=amount)
            self.assertEqual(self.send(event('refund.updated', 're_test')).status_code, 200)
            self.assertEqual(self.state()['plan'], 'premium')
        with self.sessions() as db: self.assertEqual(db.query(BillingPeriod).count(), 1)

    def test_dispute_does_not_grant_or_reset_quota(self):
        self.assertEqual(self.send(event('charge.dispute.created', 'dp_test')).status_code, 200)
        self.assertEqual(self.state()['plan'], 'free')

    def test_invalid_signatures_and_malformed_events(self):
        for signature in ('bad', 't=1,v1=bad', f't={int(time.time())+1000},v1=bad'):
            self.assertEqual(self.send(signature=signature).status_code, 401)
        for mutation in ({'livemode': False}, {'api_version': 'wrong'}, {'account': 'acct_wrong'}, {'created': 'bad'}, {'data': {}}, {'id': 'bad'}):
            value = event(); value.update(mutation)
            self.assertEqual(self.send(value).status_code, 400, mutation)
        with self.sessions() as db: self.assertEqual(db.query(BillingEvent).count(), 0)

    def test_unknown_event_is_ignored(self):
        self.assertEqual(self.send(event('unknown.event')).json()['outcome'], 'ignored_event_type')
        self.api.subscriptions.retrieve.assert_not_called()

    def test_wrong_customer_price_quantity_and_unsettled_invoice_fail_closed(self):
        original = copy.deepcopy(self.sub)
        for mutation in ('customer', 'price', 'quantity', 'owner'):
            self.sub = copy.deepcopy(original)
            if mutation == 'customer': self.sub['customer'] = 'cus_other'
            if mutation == 'price': self.sub['items']['data'][0]['price']['id'] = 'price_other'
            if mutation == 'quantity': self.sub['items']['data'][0]['quantity'] = 2
            if mutation == 'owner': self.sub['metadata']['user_id'] = '2'
            self.assertIn(self.send().status_code, (400, 503))
        self.sub = original; self.api.payment_intents.retrieve.return_value['status'] = 'processing'
        self.assertEqual(self.send().status_code, 503)
        self.assertEqual(self.state()['plan'], 'free')

    def test_out_of_band_or_zero_invoice_cannot_grant(self):
        for mutation in ({'amount_paid': 0}, {'paid_out_of_band': True}):
            self.invoices[INVOICE] = invoice(self.start, self.end); self.invoices[INVOICE].update(mutation)
            self.assertEqual(self.send().status_code, 400)

    def test_webhook_before_session_response_uses_server_binding(self):
        with self.sessions() as db:
            bound = db.get(StripeCheckout, 'bound'); bound.session_id = bound.subscription_id = None; bound.state = 'creating'; db.commit()
        self.assertEqual(self.send().status_code, 200)
        self.assertEqual(self.state()['plan'], 'premium')

    def test_unknown_binding_is_retryable(self):
        with self.sessions() as db: db.get(StripeCheckout, 'bound').subscription_id = None; db.commit()
        self.sub['metadata']['checkout_binding'] = 'missing'
        self.assertEqual(self.send().status_code, 503)

    def test_checkout_authentication_reuse_and_server_values(self):
        self.assertEqual(self.client.post('/payments/stripe/checkout').status_code, 401)
        self.assertEqual(self.client.post('/payments/stripe/checkout', headers=self.owner).json()['session_id'], SESSION)
        self.api.checkout.sessions.create.assert_not_called()
        r = self.client.post('/payments/stripe/checkout', headers=self.other, json={'price': 'wrong', 'user_id': 1, 'quantity': 9})
        self.assertEqual(r.status_code, 200, r.text)
        params = self.api.checkout.sessions.create.call_args.args[0]
        self.assertEqual(params['line_items'], [{'price': PRICE, 'quantity': 1}])
        self.assertEqual(params['metadata']['user_id'], '2')
        self.assertEqual(params['ui_mode'], 'embedded_page')
        self.assertEqual(params['mode'], 'subscription')
        self.assertNotIn('payment_method_types', params)

    def test_stripe_validation_failure_logs_safe_metadata_and_retry_reuses_binding(self):
        import stripe
        error = stripe.InvalidRequestError('private-email@example.com sk_live_sensitive cs_live_test_secret_sensitive',
            'payment_method_types', http_status=400, headers={'request-id':'req_isolated'})
        success = self.api.checkout.sessions.create.return_value
        self.api.checkout.sessions.create.side_effect = [error, success]
        with self.assertLogs('modules.stripe_billing', level='WARNING') as logs:
            response = self.client.post('/payments/stripe/checkout', headers=self.other)
        self.assertEqual(response.status_code, 502)
        diagnostic = json.loads(logs.output[0].split('Stripe checkout failed ', 1)[1])
        self.assertEqual(diagnostic, {'stage':'session_create','exception_class':'InvalidRequestError',
            'http_status':400,'param':'payment_method_types','request_id':'req_isolated'})
        for value in ('private-email@example.com','sk_live_sensitive','cs_live_test_secret_sensitive'):
            self.assertNotIn(value, str(logs.output)+response.text)
        self.assertEqual(self.client.post('/payments/stripe/checkout', headers=self.other).status_code,200)
        keys = [call.kwargs['options']['idempotency_key'] for call in self.api.checkout.sessions.create.call_args_list]
        self.assertEqual(len(set(keys)),1)
        self.assertEqual(self.api.customers.create.call_count,1)
        with self.sessions() as db:
            self.assertEqual(db.query(StripeCheckout).filter_by(user_id=2).count(),1)

    def test_checkout_status_ownership(self):
        self.assertEqual(self.client.get('/payments/stripe/checkout/' + SESSION, headers=self.owner).status_code, 200)
        self.assertEqual(self.client.get('/payments/stripe/checkout/' + SESSION, headers=self.other).status_code, 404)

    def test_checkout_ambiguous_retry_keeps_idempotency_binding(self):
        self.api.checkout.sessions.create.side_effect = RuntimeError('do not expose provider response')
        for _ in range(2): self.assertEqual(self.client.post('/payments/stripe/checkout', headers=self.other).status_code, 502)
        keys = [c.kwargs['options']['idempotency_key'] for c in self.api.checkout.sessions.create.call_args_list]
        self.assertEqual(len(set(keys)), 1)
        with self.sessions() as db: self.assertEqual(db.query(StripeCheckout).filter_by(user_id=2).count(), 1)

    def test_stale_ambiguous_intent_requires_review(self):
        with self.sessions() as db:
            row = db.get(StripeCheckout, 'bound'); row.session_id = None; row.created_at = datetime.utcnow() - timedelta(days=2); db.commit()
        self.assertEqual(self.client.post('/payments/stripe/checkout', headers=self.owner).status_code, 409)
        self.api.checkout.sessions.create.assert_not_called()

    def test_expired_session_allows_new_intent(self):
        with self.sessions() as db: db.get(StripeCheckout, 'bound').subscription_id = None; db.commit()
        self.session.update(status='expired', subscription=None)
        self.assertEqual(self.client.post('/payments/stripe/checkout', headers=self.owner).status_code, 200)
        with self.sessions() as db: self.assertEqual(db.get(StripeCheckout, 'bound').state, 'expired')

    def test_portal_strict_ownership_and_return_url(self):
        self.assertEqual(self.client.post('/payments/stripe/portal', headers=self.other, json={'customer': CUSTOMER}).status_code, 409)
        self.assertEqual(self.client.post('/payments/stripe/portal').status_code, 401)
        self.send()
        r = self.client.post('/payments/stripe/portal', headers=self.owner, json={'customer': 'cus_other'})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.api.billing_portal.sessions.create.call_args.args[0], {
            'customer': CUSTOMER, 'configuration': ENV['STRIPE_PORTAL_CONFIGURATION_ID'],
            'return_url': ENV['STRIPE_PORTAL_RETURN_URL']})
        self.assertEqual(set(r.json()), {'url'})
        self.assertTrue(self.state()['can_manage_subscription'])
        for private in (CUSTOMER, SUB, SECRET): self.assertNotIn(private, json.dumps(self.state()))

    def test_portal_requires_current_stripe_subscription_not_historical_checkout(self):
        self.assertFalse(self.state()['can_manage_subscription'])
        self.assertEqual(self.client.post('/payments/stripe/portal', headers=self.owner).status_code, 409)
        self.send()
        for provider, status, sub_id in [('lava', 'active', SUB), ('paddle', 'active', SUB),
                (None, 'active', None), ('stripe', 'canceled', SUB), ('stripe', 'active', None)]:
            with self.sessions() as db:
                u = db.get(User, 1); u.payment_provider = provider; u.subscription_status = status
                u.provider_customer_id = CUSTOMER if provider else None
                u.provider_subscription_id = sub_id; db.commit()
            self.assertFalse(self.state()['can_manage_subscription'])
            self.assertEqual(self.client.post('/payments/stripe/portal', headers=self.owner).status_code, 409)
        self.api.billing_portal.sessions.create.assert_not_called()

    def test_portal_binding_is_scoped_to_live_account_and_customer(self):
        self.send()
        for field, bad in [('livemode', False), ('account_id', 'acct_other'), ('customer_id', 'cus_other'), ('user_id', 2)]:
            with self.sessions() as db:
                row = db.get(BillingCustomer, 'customer-binding'); original = getattr(row, field)
                setattr(row, field, bad); db.commit()
            self.assertFalse(self.state()['can_manage_subscription'])
            self.assertEqual(self.client.post('/payments/stripe/portal', headers=self.owner).status_code, 409)
            with self.sessions() as db:
                setattr(db.get(BillingCustomer, 'customer-binding'), field, original); db.commit()
        self.api.billing_portal.sessions.create.assert_not_called()

    def test_portal_rechecks_remote_subscription_before_creating_session(self):
        self.send()
        original = copy.deepcopy(self.sub)
        for mutation in ({'customer': 'cus_other'}, {'status': 'canceled'}, {'livemode': False}):
            self.sub = {**original, **mutation}
            self.assertIn(self.client.post('/payments/stripe/portal', headers=self.owner).status_code, (409, 502))
        self.api.billing_portal.sessions.create.assert_not_called()

    def test_portal_scheduled_cancel_retains_paid_access_and_past_due_management(self):
        self.send(); self.sub['cancel_at'] = timestamp(self.end)
        self.send(event('customer.subscription.updated', SUB))
        state = self.state()
        self.assertEqual((state['plan'], state['gpt_messages_limit'], state['saved_charts_limit']), ('premium', 300, 10))
        self.assertTrue(state['can_manage_subscription']); self.assertTrue(state['cancel_at_period_end'])
        self.assertTrue(state['scheduled_cancel_at'])
        self.assertEqual(self.client.post('/payments/stripe/portal', headers=self.owner).status_code, 200)
        with self.sessions() as db:
            db.get(User, 1).subscription_status = 'past_due'; db.commit()
        self.sub['status'] = 'past_due'
        self.assertTrue(self.state()['can_manage_subscription'])
        self.assertEqual(self.client.post('/payments/stripe/portal', headers=self.owner).status_code, 200)
        with self.sessions() as db:
            db.get(User, 1).scheduled_cancel_at = datetime.utcnow() - timedelta(seconds=1); db.commit()
        self.assertFalse(self.state()['can_manage_subscription'])
        self.assertEqual(self.client.post('/payments/stripe/portal', headers=self.owner).status_code, 409)

    def test_switch_and_duplicate_subscription_do_not_overwrite_primary(self):
        with self.sessions() as db:
            u = db.get(User, 1); u.payment_provider = 'lava'; u.provider_subscription_id = 'lava-primary'
            usage.apply_plan(db, u, 'premium', self.start, self.end); db.commit()
        self.assertEqual(self.client.post('/payments/stripe/checkout', headers=self.owner).status_code, 409)
        self.assertEqual(self.send().json()['outcome'], 'billing_conflict')
        self.sub.update(status='canceled', ended_at=int(time.time()))
        self.send(event('customer.subscription.deleted', SUB))
        self.send(event('refund.updated', 're_test'))
        with self.sessions() as db:
            self.assertEqual(db.get(User, 1).payment_provider, 'lava')
            self.assertEqual(db.get(User, 1).current_period_end, self.end)
            self.assertEqual(db.query(BillingConflict).count(), 1)
            self.assertFalse(db.query(BillingPeriod).one().applied)

    def test_database_failure_rolls_back_event_period_and_entitlement(self):
        with patch.object(billing, 'apply_subscription_access', side_effect=RuntimeError()):
            self.assertEqual(self.send().status_code, 500)
        with self.sessions() as db:
            self.assertEqual(db.query(BillingEvent).count(), 0)
            self.assertEqual(db.query(BillingPeriod).count(), 0)
            self.assertEqual(db.get(User, 1).plan, 'free')
        self.assertEqual(self.send().status_code, 200)

    def test_overlap_rejected_without_losing_retry(self):
        self.send()
        self.invoices['in_overlap'] = invoice(self.start + timedelta(days=1), self.end + timedelta(days=1), 'in_overlap')
        self.assertEqual(self.send(event(object_id='in_overlap')).status_code, 400)
        with self.sessions() as db: self.assertEqual(db.query(BillingPeriod).count(), 1)

    def test_live_guard_and_rollout_switch(self):
        with patch.dict(os.environ, {'STRIPE_LIVEMODE': 'false'}):
            self.assertFalse(billing.configured())
        with patch.dict(os.environ, {'STRIPE_CHECKOUT_ENABLED': 'false'}):
            self.assertEqual(self.client.post('/payments/stripe/checkout', headers=self.owner).status_code, 503)
            self.assertEqual(self.send().status_code, 200)

    def test_recovery_of_paid_period_restores_access_without_resetting_usage(self):
        self.send()
        with self.sessions() as db:
            db.add(GPTUsage(user_id=1, chart_id=1, status='succeeded', plan='premium', period_start=self.start, period_end=self.end)); db.commit()
        self.sub['status'] = 'unpaid'
        self.send(event('customer.subscription.updated', SUB))
        self.assertEqual(self.state()['plan'], 'free')
        self.sub['status'] = 'active'
        self.send(event('customer.subscription.updated', SUB))
        self.assertEqual(self.state()['plan'], 'premium')
        self.assertEqual(self.state()['gpt_messages_used'], 1)

    def test_terminal_state_does_not_depend_on_invoice_payment_availability(self):
        self.send()
        self.sub.update(status='canceled', ended_at=int(time.time()))
        self.api.invoices.retrieve.side_effect = RuntimeError('invoice unavailable')
        self.assertEqual(self.send(event('customer.subscription.deleted', SUB)).status_code, 200)
        self.assertEqual(self.state()['plan'], 'free')

    def test_old_active_routes_are_absent(self):
        routes = [r.path for r in fixtures.app.routes]
        self.assertFalse(any('/payments/paddle' in p for p in routes))
