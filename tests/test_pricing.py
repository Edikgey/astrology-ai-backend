"""PRICING-1 regression, backend (SQLite part). Written before the code; red on 393158f except GUARD cases.

Source: tasks/PRICING-1/DESIGN.md @ 70408f8 (§2 packs, §3 annual variant A, §4 API and analytics, §5 names,
§9 BE-1..BE-13, §10 slice 0) and DECISIONS №28: annual $69.99 / 4 790 ₽, pack +50 questions $4.99 / 399 ₽,
annual = 300 questions in each month of the year, packs never expire, refund/dispute removes only the unused
remainder, no trial, monthly unchanged ($9.99 / 799 ₽).

Real API, real billing and quota modules, isolated SQLite. Fakes only: Stripe objects come from a Mock client
(no network), Lava answers from a patched lava_client.request, OpenAI is never called. No .env, no .secrets,
no real keys, no environment dumps. Concurrency on real row locks lives in test_pricing_postgres.py.

Contract the tests rely on (beyond DESIGN, chosen to match the existing billing tables; see REGRESSION.md):
- question_packs.id and gpt_usage.pack_id are string UUIDs (VARCHAR), like stripe_checkouts.id;
- the quota clock is modules.usage.utcnow (the existing server clock seam);
- a Stripe pack is verified from the retrieved Checkout Session (amount_total, currency, payment_status,
  payment_intent, line_items expanded or via sessions.list_line_items / sessions.line_items.list) and the
  retrieved PaymentIntent; the PaymentIntent carries metadata.pack_id (payment_intent_data at checkout).
GUARD = pins today's behaviour, passes on the base by design.
"""
import copy
import hashlib
import hmac
import json
import os
import time
import unittest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import MetaData, Table, select, text

import test_my_charts as fixtures
import test_stripe as sf
import test_lava as lf
from database.queries import User, StripeCheckout, BillingCustomer, BillingEvent, BillingPeriod, GPTUsage, LavaCheckout
from modules import stripe_billing, usage
from modules import product_analytics_schema as schema

FLAG = 'LUNARIA_FREE_BETA_ENABLED'
PACK_SIZE = 50
STRIPE_MONTH_CENTS, STRIPE_ANNUAL_CENTS, STRIPE_PACK_CENTS = 999, 6999, 499
LAVA_MONTH_RUB, LAVA_ANNUAL_RUB, LAVA_PACK_RUB = 799, 4790, 399
ANNUAL_PRICE, PACK_PRICE = 'price_annualIsolated', 'price_packIsolated'
ANNUAL_OFFER, ANNUAL_PRODUCT, PACK_OFFER, PACK_PRODUCT = [str(uuid4()) for _ in range(4)]
BUYER = 'b@example.test'  # user 2 in test_my_charts

ENV = {**sf.ENV, **lf.ENV, FLAG: 'false',
       'STRIPE_PREMIUM_ANNUAL_PRICE_ID': ANNUAL_PRICE, 'STRIPE_QUESTION_PACK_PRICE_ID': PACK_PRICE,
       'LAVA_ANNUAL_OFFER_ID': ANNUAL_OFFER, 'LAVA_PACK_OFFER_ID': PACK_OFFER}
# Slice 0: the loopback stand in Stripe test mode. Fake key, never a real credential.
TEST_ENV = {**ENV, 'STRIPE_SECRET_KEY': 'sk_test_isolated_not_a_credential', 'STRIPE_LIVEMODE': 'false',
            'STRIPE_CHECKOUT_RETURN_URL': 'http://localhost:3000/my-charts?billing=stripe&session_id={CHECKOUT_SESSION_ID}',
            'STRIPE_PORTAL_RETURN_URL': 'http://localhost:3000/my-charts?billing=stripe'}

UNAVAILABLE_OPTION = 'Этот вариант оплаты пока недоступен'
BETA_PACK = 'пакет не нужен'
BETA_YEAR = 'Годовой тариф появится после окончания бесплатной беты'
HAS_PREMIUM_YEAR = 'У вас уже есть Premium. Годовой тариф можно оформить'
LAVA_OTHER_INVOICE = 'В Lava уже открыта оплата другого тарифа Premium'
NOT_AGAIN = 'Не оплачивайте повторно'


def with_livemode(value, livemode):
    if isinstance(value, dict):
        return {k: (livemode if k == 'livemode' else with_livemode(v, livemode)) for k, v in value.items()}
    if isinstance(value, list):
        return [with_livemode(v, livemode) for v in value]
    return value


def iso(value):
    return value.replace(microsecond=0).isoformat() + 'Z'


def parse_dt(value):
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    return result.replace(tzinfo=None) if result.tzinfo else result


def text_of(response):
    return json.dumps(response.json(), ensure_ascii=False)


def install_fakes(test, livemode=True):
    """Stripe Mock client + Lava request fake on `test`. Every object carries `livemode`."""
    test.livemode = livemode
    test.start = datetime.utcnow().replace(microsecond=0) - timedelta(days=2)
    test.end = test.start + timedelta(days=31)
    test.sub = with_livemode(sf.subscription(test.start, test.end), livemode)
    test.invoices = {sf.INVOICE: with_livemode(sf.invoice(test.start, test.end), livemode)}
    test.session = with_livemode({'id': sf.SESSION, 'livemode': True, 'status': 'open', 'mode': 'subscription',
        'customer': sf.CUSTOMER, 'subscription': sf.SUB, 'metadata': {'checkout_binding': 'bound', 'user_id': '1'},
        'client_secret': sf.SESSION + '_secret_test', 'expires_at': int(time.time()) + 3600}, livemode)
    sf.install_fake(test)
    api = test.api
    monthly = sf.price(); monthly.update(type='recurring')
    test.prices = with_livemode({sf.PRICE: monthly,
        ANNUAL_PRICE: {'id': ANNUAL_PRICE, 'livemode': True, 'active': True, 'type': 'recurring', 'currency': 'usd',
                       'unit_amount': STRIPE_ANNUAL_CENTS,
                       'recurring': {'interval': 'year', 'interval_count': 1, 'usage_type': 'licensed'}},
        PACK_PRICE: {'id': PACK_PRICE, 'livemode': True, 'active': True, 'type': 'one_time', 'currency': 'usd',
                     'unit_amount': STRIPE_PACK_CENTS, 'recurring': None}}, livemode)
    api.prices.retrieve.side_effect = lambda price_id, *a, **k: copy.deepcopy(test.prices[price_id])
    test.remote, test.created, test.keys, test.customers = {}, [], {}, {}

    def create_session(params, options=None, **_):
        key = (options or {}).get('idempotency_key')
        if key in test.keys:
            return copy.deepcopy(test.remote[test.keys[key]])
        number = len(test.created) + 1
        session_id = ('cs_live_new' if livemode else 'cs_test_new') + str(number)
        price = test.prices[params['line_items'][0]['price']]
        test.remote[session_id] = {
            'id': session_id, 'object': 'checkout.session', 'livemode': livemode, 'status': 'open',
            'mode': params.get('mode'), 'payment_status': 'unpaid', 'customer': params.get('customer'),
            'client_secret': session_id + '_secret_test', 'expires_at': int(time.time()) + 3600,
            'metadata': copy.deepcopy(params.get('metadata') or {}), 'client_reference_id': params.get('client_reference_id'),
            'subscription': None, 'payment_intent': None, 'amount_total': price['unit_amount'], 'currency': 'usd',
            'line_items': {'object': 'list', 'has_more': False, 'data': [{'id': 'li_' + str(number), 'object': 'item',
                'price': copy.deepcopy(price), 'quantity': 1, 'amount_total': price['unit_amount'], 'currency': 'usd'}]}}
        test.created.append((copy.deepcopy(params), options))
        if key:
            test.keys[key] = session_id
        return copy.deepcopy(test.remote[session_id])

    def retrieve_session(session_id, *a, **k):
        return copy.deepcopy(test.remote.get(session_id, test.session))

    def expire_session(session_id, *a, **k):
        test.remote[session_id]['status'] = 'expired'
        return copy.deepcopy(test.remote[session_id])

    def line_items(session_id, *a, **k):
        return copy.deepcopy(test.remote[session_id]['line_items'])

    def create_customer(params, options=None, **_):
        key = (options or {}).get('idempotency_key') or str(uuid4())
        test.customers.setdefault(key, 'cus_buyer' + str(len(test.customers) + 1))
        return {'id': test.customers[key], 'livemode': livemode}

    api.checkout.sessions.create.side_effect = create_session
    api.checkout.sessions.retrieve.side_effect = retrieve_session
    api.checkout.sessions.expire.side_effect = expire_session
    api.checkout.sessions.list_line_items.side_effect = line_items
    api.checkout.sessions.line_items.list.side_effect = line_items
    api.customers.create.side_effect = create_customer
    test.intents = {'pi_isolated': {'id': 'pi_isolated', 'livemode': livemode, 'status': 'succeeded', 'currency': 'usd',
                                    'customer': sf.CUSTOMER, 'amount': 999, 'amount_received': 999, 'metadata': {}}}
    test.charges = {'ch_test': {'id': 'ch_test', 'livemode': livemode, 'payment_intent': 'pi_isolated',
                                'amount_refunded': 999, 'currency': 'usd'}}
    test.refunds, test.disputes = {}, {}
    test.pi_invoices, test.invoice_amounts = {'pi_isolated': sf.INVOICE}, {}
    api.payment_intents.retrieve.side_effect = lambda pi, *a, **k: copy.deepcopy(test.intents[pi])
    api.charges.retrieve.side_effect = lambda ch, *a, **k: copy.deepcopy(test.charges[ch])
    api.refunds.retrieve.side_effect = lambda re, *a, **k: copy.deepcopy(test.refunds[re])
    api.disputes.retrieve.side_effect = lambda dp, *a, **k: copy.deepcopy(test.disputes[dp])

    def invoice_payments(params, *a, **k):
        if 'invoice' in params:
            invoice_id = params['invoice']
        else:
            invoice_id = test.pi_invoices.get((params.get('payment') or {}).get('payment_intent'))
        if invoice_id is None:
            return {'has_more': False, 'data': []}  # a one-time payment has no invoice
        return {'has_more': False, 'data': [{'id': 'inpay_test', 'invoice': invoice_id,
            'amount_paid': test.invoice_amounts.get(invoice_id, STRIPE_MONTH_CENTS),
            'payment': {'type': 'payment_intent', 'payment_intent': 'pi_isolated'}}]}
    api.invoice_payments.list.side_effect = invoice_payments

    # Lava: catalog with the existing monthly offer, the annual offer and the pack offer.
    test.lava_catalog = {'items': [
        {'id': lf.PRODUCT, 'type': 'SUBSCRIPTION', 'offers': [{'id': lf.OFFER,
            'prices': [{'currency': 'RUB', 'amount': LAVA_MONTH_RUB, 'periodicity': 'MONTHLY'}]}]},
        {'id': ANNUAL_PRODUCT, 'type': 'SUBSCRIPTION', 'offers': [{'id': ANNUAL_OFFER,
            'prices': [{'currency': 'RUB', 'amount': LAVA_ANNUAL_RUB, 'periodicity': 'PERIOD_YEAR'}]}]},
        {'id': PACK_PRODUCT, 'type': 'DIGITAL_PRODUCT', 'offers': [{'id': PACK_OFFER,
            'prices': [{'currency': 'RUB', 'amount': LAVA_PACK_RUB, 'periodicity': 'ONE_TIME'}]}]}]}
    test.lava_invoices, test.lava_subscriptions, test.lava_posts = {}, {}, []
    amounts = {lf.OFFER: LAVA_MONTH_RUB, ANNUAL_OFFER: LAVA_ANNUAL_RUB, PACK_OFFER: LAVA_PACK_RUB}

    def lava(method, path, **kwargs):
        if method == 'GET' and path.startswith('/api/v2/products'):
            return copy.deepcopy(test.lava_catalog)
        if method == 'POST' and path == '/api/v3/invoice':
            body = kwargs['json']
            test.lava_posts.append(copy.deepcopy(body))
            contract, amount = str(uuid4()), amounts[body['offerId']]
            test.lava_invoices[contract] = {'id': contract, 'type': 'INVOICE' if body['offerId'] == PACK_OFFER else 'SUBSCRIPTION',
                'status': 'NEW', 'buyer': {'email': body['email']}, 'datetime': iso(datetime.utcnow()),
                'receipt': {'amount': amount, 'currency': 'RUB'}, 'amountTotal': {'amount': amount, 'currency': 'RUB'}}
            return {'id': contract, 'paymentUrl': f'https://pay.example.test/invoice/{len(test.lava_posts)}',
                    'amountTotal': {'amount': amount, 'currency': 'RUB'}}
        if method == 'GET' and path.startswith('/api/v2/invoices/'):
            return copy.deepcopy(test.lava_invoices[path.rsplit('/', 1)[1]])
        if method == 'GET' and path.startswith('/api/v1/subscriptions/'):
            return copy.deepcopy(test.lava_subscriptions[path.rsplit('/', 1)[1]])
        if method == 'DELETE':
            return None
        raise AssertionError('unexpected Lava call')
    network = patch('modules.lava_client.request', side_effect=lava)
    test.network = network.start()
    test.addCleanup(network.stop)


class PricingBase(unittest.TestCase):
    LIVEMODE = True
    ENVIRONMENT = ENV
    tearDown = fixtures.MyChartsTests.tearDown
    chart = fixtures.MyChartsTests.chart

    def setUp(self):
        fixtures.MyChartsTests.setUp(self)
        env = patch.dict(os.environ, self.ENVIRONMENT); env.start(); self.addCleanup(env.stop)
        install_fakes(self, self.LIVEMODE)
        self.charts = {}

    # ---- HTTP
    def send(self, value):
        body = json.dumps(value).encode()
        stamp = str(int(time.time()))
        sig = hmac.new(sf.SECRET.encode(), stamp.encode() + b'.' + body, hashlib.sha256).hexdigest()
        return self.client.post('/payments/stripe/webhook', content=body, headers={'Stripe-Signature': f't={stamp},v1={sig}'})

    def send_lava(self, body):
        return self.client.post('/payments/lava/webhook', json=body, headers={'X-Api-Key': lf.SECRET})

    def event(self, kind, object_id, event_id=None):
        value = sf.event(kind, object_id, event_id)
        value['livemode'] = self.livemode
        return value

    def usage_of(self, headers):
        response = self.client.get('/account/usage', headers=headers)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def checkout(self, provider, headers, product=None, **extra):
        body = {} if product is None else {'product': product}
        body.update(extra)
        return self.client.post(f'/payments/{provider}/checkout', headers=headers, json=body if body else None)

    # ---- storage (table and column names from DESIGN §2, §5)
    def table(self, name):
        return Table(name, MetaData(), autoload_with=self.engine)

    def packs(self, user_id=None):
        packs = self.table('question_packs')
        query = select(packs).order_by(packs.c.created_at, packs.c.id)
        if user_id is not None:
            query = query.where(packs.c.user_id == user_id)
        with self.engine.connect() as conn:
            return [dict(row) for row in conn.execute(query).mappings()]

    def pack(self, pack_id):
        found = [row for row in self.packs() if row['id'] == pack_id]
        self.assertEqual(len(found), 1, 'pack row not found')
        return found[0]

    def seed_pack(self, user_id=1, quantity=PACK_SIZE, paid_at=None, state='paid', pi=None, created_at=None):
        pack_id = str(uuid4())
        paid_at = paid_at or datetime.utcnow() - timedelta(hours=1)
        values = dict(id=pack_id, user_id=user_id, provider='stripe', account_id=sf.ACCOUNT, livemode=True,
                      quantity=quantity, state=state, customer_id=sf.CUSTOMER, session_id='cs_live_seed' + pack_id[:8],
                      payment_intent_id=pi or 'pi_seed' + pack_id[:8], created_at=created_at or paid_at - timedelta(minutes=5),
                      paid_at=paid_at if state in ('paid', 'revoked') else None)
        with self.engine.begin() as conn:
            conn.execute(self.table('question_packs').insert().values(**values))
        return pack_id

    def pack_uses(self, pack_id):
        with self.engine.connect() as conn:
            return conn.execute(text("SELECT count(*) FROM gpt_usage WHERE pack_id = :p AND status = 'succeeded'"),
                                {'p': pack_id}).scalar_one()

    def plan_uses(self, user_id=1):
        with self.engine.connect() as conn:
            return conn.execute(text("SELECT count(*) FROM gpt_usage WHERE user_id = :u AND pack_id IS NULL "
                                     "AND status = 'succeeded'"), {'u': user_id}).scalar_one()

    def set_interval(self, user_id, value):
        with self.engine.begin() as conn:
            conn.execute(text('UPDATE users SET current_period_interval = :v WHERE id = :u'), {'v': value, 'u': user_id})

    def interval(self, user_id):
        with self.engine.connect() as conn:
            return conn.execute(text('SELECT current_period_interval FROM users WHERE id = :u'), {'u': user_id}).scalar_one()

    # ---- quota helpers (existing reserve -> finalize path)
    def chart_for(self, user_id):
        if user_id not in self.charts:
            self.charts[user_id] = self.chart(user_id=user_id, related=False)
        return self.charts[user_id]

    def seed_usage(self, count, user_id=1, plan='free', start=None, end=None, created_at=None):
        with self.sessions() as db:
            db.add_all([GPTUsage(user_id=user_id, chart_id=1000, status='succeeded', plan=plan, period_start=start,
                                 period_end=end, created_at=created_at or datetime.utcnow()) for _ in range(count)])
            db.commit()

    def reserve(self, user_id=1):
        with self.sessions() as db:
            return usage.reserve(db, user_id, self.chart_for(user_id))

    def finalize(self, reservation, user_id=1):
        with self.sessions() as db:
            usage.finalize(db, user_id, reservation, 'Вопрос', 'Ответ')

    def spend(self, user_id=1):
        self.finalize(self.reserve(user_id), user_id)

    def refused(self, user_id=1):
        with self.assertRaises(HTTPException) as caught:
            self.reserve(user_id)
        self.assertEqual(caught.exception.status_code, 409)
        return caught.exception.detail

    def premium(self, user_id=1, start=None, end=None):
        now = datetime.utcnow()
        self.pstart, self.pend = start or now - timedelta(days=1), end or now + timedelta(days=29)
        with self.sessions() as db:
            usage.set_plan(db, user_id, 'premium', self.pstart, self.pend)

    # ---- Stripe pack flow
    def buy_stripe_pack(self, headers=None):
        response = self.checkout('stripe', headers or self.other, 'question_pack')
        self.assertEqual(response.status_code, 200, response.text)
        session_id = response.json()['session_id']
        self.assertIn('pack_id', self.remote[session_id]['metadata'], 'no pack session was created (product ignored?)')
        return session_id, self.remote[session_id]['metadata']['pack_id']

    def pay_stripe_pack(self, session_id, pi='pi_pack', status='succeeded'):
        remote = self.remote[session_id]
        remote.update(status='complete', payment_status='paid', payment_intent=pi)
        params = next(p for p, _ in self.created if p['metadata'] == remote['metadata'])
        self.intents[pi] = {'id': pi, 'object': 'payment_intent', 'livemode': self.livemode, 'status': status,
            'amount': STRIPE_PACK_CENTS, 'amount_received': STRIPE_PACK_CENTS, 'currency': 'usd', 'customer': remote['customer'],
            'metadata': copy.deepcopy((params.get('payment_intent_data') or {}).get('metadata') or {})}
        self.charges['ch_' + pi[3:]] = {'id': 'ch_' + pi[3:], 'livemode': self.livemode, 'payment_intent': pi,
            'amount': STRIPE_PACK_CENTS, 'amount_refunded': 0, 'currency': 'usd', 'customer': remote['customer']}
        return self.event('checkout.session.completed', session_id)

    def refund_stripe(self, pi, amount=STRIPE_PACK_CENTS, kind='refund.created'):
        refund_id = 're_' + pi[3:] + str(len(self.refunds))
        charge = 'ch_' + pi[3:]
        self.charges.setdefault(charge, {'id': charge, 'livemode': self.livemode, 'payment_intent': pi,
                                         'amount': STRIPE_PACK_CENTS, 'currency': 'usd'})
        self.charges[charge]['amount_refunded'] = amount
        self.refunds[refund_id] = {'id': refund_id, 'livemode': self.livemode, 'charge': charge, 'amount': amount,
                                   'currency': 'usd', 'status': 'succeeded'}
        return self.send(self.event(kind, refund_id))

    def dispute_stripe(self, pi, kind='charge.dispute.created', status='needs_response'):
        dispute_id = 'dp_' + pi[3:]
        self.disputes[dispute_id] = {'id': dispute_id, 'livemode': self.livemode, 'charge': 'ch_' + pi[3:],
                                     'amount': STRIPE_PACK_CENTS, 'currency': 'usd', 'status': status}
        return self.send(self.event(kind, dispute_id))

    def bind_subscription(self, user_id=1, price_id=None):
        """The existing fixture binding ('bound'); price_id is stripe_checkouts.price_id (DESIGN §5)."""
        with self.sessions() as db:
            db.add(BillingCustomer(id='customer-binding', user_id=user_id, provider='stripe', account_id=sf.ACCOUNT,
                                   livemode=self.livemode, customer_id=sf.CUSTOMER))
            db.add(StripeCheckout(id='bound', user_id=user_id, account_id=sf.ACCOUNT, livemode=self.livemode,
                                  customer_id=sf.CUSTOMER, session_id=sf.SESSION, subscription_id=sf.SUB, state='ready'))
            db.commit()
        if price_id:
            with self.engine.begin() as conn:
                conn.execute(text("UPDATE stripe_checkouts SET price_id = :p WHERE id = 'bound'"), {'p': price_id})

    # ---- Lava flow
    def lava_event(self, contract, product, amount, status, kind='payment.success', at=None, email=BUYER, parent=None):
        value = {'eventType': kind, 'contractId': contract, 'timestamp': iso(at or datetime.utcnow()),
                 'buyer': {'email': email}, 'product': {'id': product}, 'amount': amount, 'currency': 'RUB', 'status': status}
        if parent:
            value['parentContractId'] = parent
        return value

    def lava_financial(self, kind='refund.success', email=BUYER, offer=PACK_OFFER, product=PACK_PRODUCT, amount=LAVA_PACK_RUB,
                       at=None, partial=False):
        data = {'amount': amount, 'currency': 'RUB', 'subscription_cancelled': False, 'customer_email': email,
                'product': {'product_id': product, 'tier_id': offer}}
        if kind == 'refund.success':
            data.update(refund_id=str(uuid4()), refund_type='partial' if partial else 'full')
        else:
            data.update(chargeback_id=str(uuid4()), dispute_date='2026-11-14')
        return {'event_id': str(uuid4()), 'event_type': kind, 'created_at': iso(at or datetime.utcnow() + timedelta(seconds=5)), 'data': data}

    def buy_lava_pack(self, headers=None):
        response = self.checkout('lava', headers or self.other, 'question_pack')
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.lava_posts[-1]['offerId'], PACK_OFFER, 'no pack invoice was created (product ignored?)')
        contract = [c for c, inv in self.lava_invoices.items() if inv['type'] == 'INVOICE'][-1]
        return contract, response.json()['url']

    def grant_lava_pack(self, contract):
        self.lava_invoices[contract]['status'] = 'COMPLETED'
        response = self.send_lava(self.lava_event(contract, PACK_PRODUCT, LAVA_PACK_RUB, 'completed'))
        self.assertEqual(response.status_code, 200, response.text)
        return response


# =========================================================================== S0 — Stripe test mode on the loopback stand
class PricingS0TestModeTests(PricingBase):
    """DESIGN §10 «Срез 0»: STRIPE_LIVEMODE exactly 'true' or exactly 'false'; 'false' only with sk_test_,
    livemode=false objects, own storage and event scope, return URLs only http://localhost:3000 or
    http://127.0.0.1:3000; anything else 503. Production variables give today's decisions."""
    LIVEMODE = False
    ENVIRONMENT = TEST_ENV

    def test_s0_1_test_mode_is_configured_on_loopback(self):
        self.assertTrue(stripe_billing.configured())
        self.assertTrue(self.usage_of(self.other)['stripe_checkout_available'])

    def test_s0_2_test_checkout_creates_test_objects_and_rows(self):
        response = self.checkout('stripe', self.other)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()['session_id'].startswith('cs_test_'))
        params, _ = self.created[0]
        self.assertTrue(params['return_url'].startswith('http://localhost:3000/'))
        self.assertEqual(params['line_items'], [{'price': sf.PRICE, 'quantity': 1}])
        with self.sessions() as db:
            self.assertEqual([c.livemode for c in db.query(StripeCheckout).filter_by(user_id=2)], [False])
            self.assertEqual([c.livemode for c in db.query(BillingCustomer).filter_by(user_id=2)], [False])

    def test_s0_3_loopback_ip_return_url_is_accepted(self):
        with patch.dict(os.environ, {'STRIPE_CHECKOUT_RETURN_URL': 'http://127.0.0.1:3000/my-charts?billing=stripe&session_id={CHECKOUT_SESSION_ID}'}):
            self.assertEqual(self.checkout('stripe', self.other).status_code, 200)

    def test_s0_4_guard_test_mode_refuses_live_keys_and_non_loopback_urls(self):
        # GUARD (base refuses every test-mode value); paired with S0-2 which must turn green.
        cases = [{'STRIPE_SECRET_KEY': 'sk_live_isolated_not_a_credential'},
                 {'STRIPE_SECRET_KEY': 'rk_live_isolated_not_a_credential'},
                 {'STRIPE_SECRET_KEY': 'pk_test_isolated_not_a_credential'},
                 {'STRIPE_CHECKOUT_RETURN_URL': 'https://mylunariaai.com/my-charts?billing=stripe'},
                 {'STRIPE_CHECKOUT_RETURN_URL': 'http://localhost:3001/my-charts'},
                 {'STRIPE_CHECKOUT_RETURN_URL': 'https://localhost:3000/my-charts'},
                 {'STRIPE_CHECKOUT_RETURN_URL': 'http://localhost/my-charts'},
                 {'STRIPE_CHECKOUT_RETURN_URL': 'http://user@localhost:3000/my-charts'},
                 {'STRIPE_CHECKOUT_RETURN_URL': 'http://localhost.evil.test:3000/my-charts'},
                 {'STRIPE_CHECKOUT_RETURN_URL': 'http://192.168.1.5:3000/my-charts'}]
        for mutation in cases:
            with self.subTest(mutation=mutation), patch.dict(os.environ, mutation):
                self.assertEqual(self.checkout('stripe', self.other).status_code, 503)
        self.api.customers.create.assert_not_called()
        self.api.checkout.sessions.create.assert_not_called()

    def test_s0_5_guard_livemode_value_must_be_exact(self):
        # GUARD: only the exact strings 'true' / 'false' choose a mode.
        for value in ('True', 'FALSE', '1', '0', 'yes', ' false', 'false ', ''):
            for key in ('sk_test_isolated_not_a_credential', 'sk_live_isolated_not_a_credential'):
                with self.subTest(value=value, key=key[:7]), patch.dict(os.environ, {'STRIPE_LIVEMODE': value, 'STRIPE_SECRET_KEY': key}):
                    self.assertFalse(stripe_billing.configured())
                    self.assertEqual(self.checkout('stripe', self.other).status_code, 503)
        self.api.checkout.sessions.create.assert_not_called()

    def test_s0_7_live_objects_are_refused_in_test_mode(self):
        self.prices[sf.PRICE]['livemode'] = True
        self.assertIn(self.checkout('stripe', self.other).status_code, (502, 503))
        self.api.checkout.sessions.create.assert_not_called()
        self.prices[sf.PRICE]['livemode'] = False  # positive control
        self.assertEqual(self.checkout('stripe', self.other).status_code, 200)

    def test_s0_8_test_webhook_grants_in_test_scope(self):
        self.bind_subscription()
        response = self.send(self.event('invoice.paid', sf.INVOICE))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.usage_of(self.owner)['plan'], 'premium')
        with self.sessions() as db:
            keys = [e.event_id for e in db.query(BillingEvent)]
            self.assertEqual(len(keys), 1)
            self.assertIn(':test:', keys[0])
            self.assertNotIn(':live:', keys[0])
            self.assertEqual([p.livemode for p in db.query(BillingPeriod)], [False])

    def test_s0_9_event_from_the_other_mode_is_refused(self):
        self.bind_subscription()
        live_event = self.event('invoice.paid', sf.INVOICE); live_event['livemode'] = True
        self.assertEqual(self.send(live_event).status_code, 400)
        self.assertEqual(self.usage_of(self.owner)['plan'], 'free')
        self.assertEqual(self.send(self.event('invoice.paid', sf.INVOICE)).status_code, 200)  # positive control

    def test_s0_10_live_rows_and_events_are_not_reused_in_test_mode(self):
        with self.sessions() as db:
            db.add(BillingCustomer(id='live-customer', user_id=1, provider='stripe', account_id=sf.ACCOUNT, livemode=True, customer_id=sf.CUSTOMER))
            db.add(StripeCheckout(id='live-bound', user_id=1, account_id=sf.ACCOUNT, livemode=True, customer_id=sf.CUSTOMER,
                                  session_id=sf.SESSION, state='ready'))
            db.add(BillingEvent(provider='stripe', event_id='stripe:' + sf.ACCOUNT + ':live:evt_shared', event_type='invoice.paid',
                                occurred_at=datetime.utcnow(), outcome='processed'))
            db.commit()
        response = self.checkout('stripe', self.owner)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotEqual(response.json()['session_id'], sf.SESSION)
        self.assertEqual(self.api.customers.create.call_count, 1)
        with self.sessions() as db:
            self.assertEqual(sorted(c.livemode for c in db.query(BillingCustomer).filter_by(user_id=1)), [False, True])
        self.bind_test_mode_binding_for_user_2()
        result = self.send(self.event('invoice.paid', sf.INVOICE, 'evt_shared'))
        self.assertEqual(result.status_code, 200, result.text)
        self.assertNotEqual(result.json()['outcome'], 'duplicate')

    def bind_test_mode_binding_for_user_2(self):
        self.sub['metadata']['user_id'] = '2'
        self.session['metadata']['user_id'] = '2'
        with self.sessions() as db:
            db.add(BillingCustomer(id='test-customer-2', user_id=2, provider='stripe', account_id=sf.ACCOUNT, livemode=False, customer_id=sf.CUSTOMER))
            db.add(StripeCheckout(id='bound', user_id=2, account_id=sf.ACCOUNT, livemode=False, customer_id=sf.CUSTOMER,
                                  subscription_id=sf.SUB, state='ready'))
            db.commit()


class PricingS0ProductionGuardTests(PricingBase):
    def test_s0_6_guard_live_mode_refuses_test_key_and_loopback_url(self):
        # GUARD: production decisions are unchanged.
        for mutation in ({'STRIPE_SECRET_KEY': 'sk_test_isolated_not_a_credential'},
                         {'STRIPE_CHECKOUT_RETURN_URL': 'http://localhost:3000/my-charts?billing=stripe'}):
            with self.subTest(mutation=mutation), patch.dict(os.environ, mutation):
                self.assertEqual(self.checkout('stripe', self.other).status_code, 503)
        self.api.checkout.sessions.create.assert_not_called()

    def test_s0_11_guard_production_variables_keep_todays_checkout(self):
        # GUARD: with production variables the monthly checkout is exactly today's.
        response = self.checkout('stripe', self.other)
        self.assertEqual(response.status_code, 200, response.text)
        params, options = self.created[0]
        self.assertEqual((params['mode'], params['line_items'], params['return_url']),
                         ('subscription', [{'price': sf.PRICE, 'quantity': 1}], sf.ENV['STRIPE_CHECKOUT_RETURN_URL']))
        self.assertTrue(options['idempotency_key'].startswith('checkout:'))
        with self.sessions() as db:
            self.assertEqual([c.livemode for c in db.query(StripeCheckout).filter_by(user_id=2)], [True])


# =========================================================================== BE-1 — order of spending (DESIGN §2 table)
class PricingBE1SpendingOrderTests(PricingBase):
    def test_be1_beta_is_unlimited_and_never_spends_a_pack(self):
        pack = self.seed_pack()
        self.seed_usage(10)
        with patch.dict(os.environ, {FLAG: 'true'}):
            self.spend()
            self.assertEqual(self.usage_of(self.owner)['pack_questions_remaining'], PACK_SIZE)
        self.assertEqual(self.pack_uses(pack), 0)
        self.assertEqual(self.plan_uses(), 11)

    def test_be1_free_9_of_10_uses_the_free_question_first(self):
        pack = self.seed_pack()
        self.seed_usage(9)
        self.spend()
        self.assertEqual((self.pack_uses(pack), self.plan_uses()), (0, 10))
        self.assertEqual(self.usage_of(self.owner)['pack_questions_remaining'], PACK_SIZE)

    def test_be1_free_10_of_10_uses_the_pack_and_free_count_stays(self):
        pack = self.seed_pack()
        self.seed_usage(10)
        self.spend()
        self.assertEqual((self.pack_uses(pack), self.plan_uses()), (1, 10))
        state = self.usage_of(self.owner)
        self.assertEqual((state['gpt_messages_used'], state['gpt_messages_available'], state['pack_questions_remaining']),
                         (10, 0, PACK_SIZE - 1))

    def test_be1_guard_free_10_of_10_without_pack_refusal_unchanged(self):
        self.seed_usage(10)
        detail = self.refused()
        self.assertEqual((detail['code'], detail['message']), ('GPT_LIMIT_REACHED', 'Достигнут лимит GPT-сообщений аккаунта.'))

    def test_be1_premium_299_of_300_uses_premium_first(self):
        self.premium()
        pack = self.seed_pack()
        self.seed_usage(299, plan='premium', start=self.pstart, end=self.pend)
        self.spend()
        self.assertEqual((self.pack_uses(pack), self.plan_uses()), (0, 300))

    def test_be1_premium_300_of_300_uses_the_pack_and_premium_count_stays(self):
        self.premium()
        pack = self.seed_pack()
        self.seed_usage(300, plan='premium', start=self.pstart, end=self.pend)
        self.spend()
        self.assertEqual(self.pack_uses(pack), 1)
        state = self.usage_of(self.owner)
        self.assertEqual((state['gpt_messages_used'], state['pack_questions_remaining']), (300, PACK_SIZE - 1))

    def test_be1_guard_premium_300_of_300_without_pack_refusal_unchanged(self):
        self.premium()
        self.seed_usage(300, plan='premium', start=self.pstart, end=self.pend)
        self.assertEqual(self.refused()['code'], 'GPT_LIMIT_REACHED')

    def test_be1_invalid_premium_period_uses_the_pack(self):
        now = datetime.utcnow()
        self.premium(start=now - timedelta(days=40), end=now - timedelta(days=10))
        pack = self.seed_pack()
        self.spend()
        self.assertEqual(self.pack_uses(pack), 1)

    def test_be1_guard_invalid_premium_period_without_pack_refusal_unchanged(self):
        now = datetime.utcnow()
        self.premium(start=now - timedelta(days=40), end=now - timedelta(days=10))
        detail = self.refused()
        self.assertEqual((detail['code'], detail['message']), ('GPT_PERIOD_INVALID', 'Нет действующего периода Premium.'))

    def test_be1_unpaid_failed_and_revoked_packs_give_nothing(self):
        self.seed_usage(10)
        for state in ('creating', 'ready', 'failed', 'expired', 'rejected', 'revoked'):
            self.seed_pack(state=state)
        self.assertEqual(self.usage_of(self.owner)['pack_questions_remaining'], 0)
        self.assertEqual(self.refused()['code'], 'GPT_LIMIT_REACHED')
        self.seed_pack()  # positive control
        self.spend()

    def test_be1_released_reservation_returns_the_pack_question(self):
        pack = self.seed_pack(quantity=1)
        self.seed_usage(10)
        reservation = self.reserve()
        self.assertEqual(self.usage_of(self.owner)['pack_questions_remaining'], 0)
        with self.sessions() as db:
            usage.release(db, 1, reservation)
        self.assertEqual(self.usage_of(self.owner)['pack_questions_remaining'], 1)
        self.spend()
        self.assertEqual(self.pack_uses(pack), 1)

    def test_be1_http_question_from_the_pack(self):
        pack = self.seed_pack()
        self.seed_usage(10)
        chart_id = self.chart_for(1)
        with patch('api.endpoints.ChartInterpreter') as interpreter:
            interpreter.return_value.ask_gpt = AsyncMock(return_value='Answer')
            response = self.client.post('/ask-gpt', headers=self.owner, json={'chart_id': chart_id, 'question': 'Question'})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.pack_uses(pack), 1)


# =========================================================================== BE-2 — two packs
class PricingBE2TwoPacksTests(PricingBase):
    def two_packs(self):
        now = datetime.utcnow()
        old = self.seed_pack(quantity=2, paid_at=now - timedelta(days=2), pi='pi_old')
        young = self.seed_pack(quantity=2, paid_at=now - timedelta(days=1), pi='pi_young')
        return old, young

    def test_be2_oldest_pack_is_spent_first(self):
        self.seed_usage(10)
        old, young = self.two_packs()
        self.spend(); self.spend()
        self.assertEqual((self.pack_uses(old), self.pack_uses(young)), (2, 0))
        self.spend()
        self.assertEqual((self.pack_uses(old), self.pack_uses(young)), (2, 1))
        self.assertEqual(self.usage_of(self.owner)['gpt_messages_used'], 10)

    def test_be2_refund_of_younger_pack_keeps_older_remainder(self):
        self.seed_usage(10)
        old, young = self.two_packs()
        self.spend()
        self.assertEqual(self.refund_stripe('pi_young').status_code, 200)
        self.assertEqual(self.pack(young)['state'], 'revoked')
        self.assertEqual(self.pack(old)['state'], 'paid')
        self.assertEqual(self.usage_of(self.owner)['pack_questions_remaining'], 1)
        self.spend()
        self.assertEqual(self.refused()['code'], 'GPT_LIMIT_REACHED')

    def test_be2_refund_of_older_pack_removes_only_its_remainder(self):
        self.seed_usage(10)
        old, young = self.two_packs()
        self.spend()
        self.assertEqual(self.refund_stripe('pi_old').status_code, 200)
        self.assertEqual(self.pack(old)['state'], 'revoked')
        self.assertEqual(self.pack_uses(old), 1)  # the asked question stays
        self.assertEqual(self.usage_of(self.owner)['pack_questions_remaining'], 2)
        self.spend()
        self.assertEqual(self.pack_uses(young), 1)

    def test_be2_premium_count_unchanged_by_pack_questions(self):
        self.premium()
        self.seed_usage(300, plan='premium', start=self.pstart, end=self.pend)
        old, young = self.two_packs()
        for _ in range(3):
            self.spend()
        state = self.usage_of(self.owner)
        self.assertEqual((state['gpt_messages_used'], state['gpt_messages_available'], state['pack_questions_remaining']), (300, 0, 1))


# =========================================================================== BE-4 — Stripe pack, exactly one grant (sequential)
class PricingBE4StripeOneGrantTests(PricingBase):
    def test_be4_repeated_event_is_duplicate(self):
        session_id, pack_id = self.buy_stripe_pack()
        value = self.pay_stripe_pack(session_id)
        self.assertEqual(self.send(value).status_code, 200)
        self.assertEqual(self.send(value).json()['outcome'], 'duplicate')
        self.assertEqual(self.pack(pack_id)['state'], 'paid')
        self.assertEqual(self.usage_of(self.other)['pack_questions_remaining'], PACK_SIZE)

    def test_be4_completed_then_async_succeeded_grants_once(self):
        session_id, pack_id = self.buy_stripe_pack()
        self.assertEqual(self.send(self.pay_stripe_pack(session_id)).status_code, 200)
        later = self.send(self.event('checkout.session.async_payment_succeeded', session_id))
        self.assertEqual(later.status_code, 200, later.text)
        self.assertEqual(self.usage_of(self.other)['pack_questions_remaining'], PACK_SIZE)
        self.assertEqual(len(self.packs(2)), 1)

    def test_be4_two_distinct_events_for_one_session_grant_once(self):
        session_id, _ = self.buy_stripe_pack()
        self.assertEqual(self.send(self.pay_stripe_pack(session_id)).status_code, 200)
        self.assertEqual(self.send(self.event('checkout.session.completed', session_id)).status_code, 200)
        self.assertEqual(self.usage_of(self.other)['pack_questions_remaining'], PACK_SIZE)

    def test_be4_webhook_before_session_saved_binds_by_metadata(self):
        session_id, pack_id = self.buy_stripe_pack()
        with self.engine.begin() as conn:
            conn.execute(text("UPDATE question_packs SET session_id = NULL, state = 'creating' WHERE id = :p"), {'p': pack_id})
        self.assertEqual(self.send(self.pay_stripe_pack(session_id)).status_code, 200)
        row = self.pack(pack_id)
        self.assertEqual((row['state'], row['session_id']), ('paid', session_id))
        self.assertEqual(self.usage_of(self.other)['pack_questions_remaining'], PACK_SIZE)


# =========================================================================== BE-5 — Stripe pack, no confirmation, no grant
class PricingBE5StripeNoConfirmationTests(PricingBase):
    def test_be5_unconfirmed_or_wrong_payment_never_grants(self):
        session_id, pack_id = self.buy_stripe_pack()
        self.pay_stripe_pack(session_id)
        good_session, good_intent = copy.deepcopy(self.remote[session_id]), copy.deepcopy(self.intents['pi_pack'])
        cases = [  # (what, expected status, session change, intent change)
            ('unpaid', 503, {'payment_status': 'unpaid'}, {}),
            ('intent processing', 503, {}, {'status': 'processing'}),
            ('foreign price', 400, {'line_items': {'object': 'list', 'has_more': False, 'data': [{'id': 'li_x', 'price': {**self.prices[PACK_PRICE], 'id': 'price_other'},
                                                   'quantity': 1, 'amount_total': STRIPE_PACK_CENTS, 'currency': 'usd'}]}}, {}),
            ('amount', 400, {'amount_total': 100}, {'amount': 100, 'amount_received': 100}),
            ('currency', 400, {'currency': 'eur'}, {'currency': 'eur'}),
            ('livemode', 400, {'livemode': False}, {}),
            ('metadata owner', 400, {'metadata': {**good_session['metadata'], 'user_id': '1'}}, {}),
            ('customer', 400, {'customer': 'cus_other'}, {'customer': 'cus_other'}),
        ]
        for what, status, session_change, intent_change in cases:
            with self.subTest(what=what):
                self.remote[session_id] = {**copy.deepcopy(good_session), **copy.deepcopy(session_change)}
                self.intents['pi_pack'] = {**copy.deepcopy(good_intent), **intent_change}
                response = self.send(self.event('checkout.session.completed', session_id))
                self.assertEqual(response.status_code, status, response.text)
                self.assertNotEqual(self.pack(pack_id)['state'], 'paid')
                self.assertEqual(self.usage_of(self.other)['pack_questions_remaining'], 0)
        self.remote[session_id], self.intents['pi_pack'] = good_session, good_intent  # positive control
        self.assertEqual(self.send(self.event('checkout.session.completed', session_id)).status_code, 200)
        self.assertEqual(self.usage_of(self.other)['pack_questions_remaining'], PACK_SIZE)


# =========================================================================== BE-6 — Stripe refund and dispute of a pack
class PricingBE6StripeRefundTests(PricingBase):
    def granted_pack(self):
        session_id, pack_id = self.buy_stripe_pack()
        self.assertEqual(self.send(self.pay_stripe_pack(session_id)).status_code, 200)
        return session_id, pack_id

    def test_be6_refund_after_partial_use_removes_only_the_remainder(self):
        _, pack_id = self.granted_pack()
        self.seed_usage(10, user_id=2)
        for _ in range(3):
            self.spend(2)
        self.assertEqual(self.refund_stripe('pi_pack').status_code, 200)
        row = self.pack(pack_id)
        self.assertEqual((row['state'], row['revoke_reason']), ('revoked', 'refund'))
        self.assertIsNotNone(row['revoked_at'])
        self.assertEqual(self.pack_uses(pack_id), 3)
        self.assertEqual(self.usage_of(self.other)['pack_questions_remaining'], 0)
        self.assertEqual(self.refused(2)['code'], 'GPT_LIMIT_REACHED')

    def test_be6_partial_refund_also_revokes(self):
        _, pack_id = self.granted_pack()
        self.assertEqual(self.refund_stripe('pi_pack', amount=100).status_code, 200)
        self.assertEqual(self.pack(pack_id)['state'], 'revoked')

    def test_be6_refund_before_grant_blocks_late_completion(self):
        session_id, pack_id = self.buy_stripe_pack()
        late = self.pay_stripe_pack(session_id)  # PaymentIntent knows metadata.pack_id; the pack has no intent id yet
        self.assertEqual(self.refund_stripe('pi_pack').status_code, 200)
        self.assertEqual(self.pack(pack_id)['state'], 'revoked')
        self.assertEqual(self.send(late).status_code, 200)
        self.assertEqual(self.pack(pack_id)['state'], 'revoked')
        self.assertEqual(self.usage_of(self.other)['pack_questions_remaining'], 0)

    def test_be6_dispute_revokes_and_closing_does_not_restore(self):
        _, pack_id = self.granted_pack()
        self.assertEqual(self.dispute_stripe('pi_pack').status_code, 200)
        row = self.pack(pack_id)
        self.assertEqual((row['state'], row['revoke_reason']), ('revoked', 'dispute'))
        self.assertEqual(self.dispute_stripe('pi_pack', 'charge.dispute.closed', 'won').status_code, 200)
        self.assertEqual(self.pack(pack_id)['state'], 'revoked')
        self.assertEqual(self.usage_of(self.other)['pack_questions_remaining'], 0)

    def test_be6_subscription_refund_still_audit_only(self):
        # Subscription refunds stay records only (as test_stripe); a pack in the same account is untouched.
        self.bind_subscription()
        self.assertEqual(self.send(self.event('invoice.paid', sf.INVOICE)).json()['outcome'], 'granted')
        pack = self.seed_pack()
        self.refunds['re_sub'] = {'id': 're_sub', 'livemode': True, 'charge': 'ch_test', 'amount': 999, 'currency': 'usd', 'status': 'succeeded'}
        self.assertEqual(self.send(self.event('refund.created', 're_sub')).status_code, 200)
        self.assertEqual(self.usage_of(self.owner)['plan'], 'premium')
        self.assertEqual(self.pack(pack)['state'], 'paid')


# =========================================================================== BE-7 — Lava pack
class PricingBE7LavaPackTests(PricingBase):
    def test_be7_pack_checkout_is_a_one_time_invoice(self):
        contract, url = self.buy_lava_pack()
        body = self.lava_posts[-1]
        self.assertEqual((body['offerId'], body['periodicity'], body['currency'], body['email']), (PACK_OFFER, 'ONE_TIME', 'RUB', BUYER))
        self.assertNotIn('amount', body)
        row, = self.packs(2)
        self.assertEqual((row['provider'], row['state'], row['contract_id'], row['quantity'], row['payment_url']),
                         ('lava', 'ready', contract, PACK_SIZE, url))
        with self.sessions() as db:
            self.assertEqual(db.query(LavaCheckout).filter_by(user_id=2).count(), 0)  # not a subscription attempt

    def test_be7_paid_pack_is_granted_once(self):
        contract, _ = self.buy_lava_pack()
        self.lava_invoices[contract]['status'] = 'COMPLETED'
        value = self.lava_event(contract, PACK_PRODUCT, LAVA_PACK_RUB, 'completed')
        self.assertEqual(self.send_lava(value).status_code, 200)
        self.assertEqual(self.send_lava(value).json()['outcome'], 'duplicate')
        again = self.lava_event(contract, PACK_PRODUCT, LAVA_PACK_RUB, 'completed', at=datetime.utcnow() + timedelta(minutes=1))
        self.assertEqual(self.send_lava(again).status_code, 200)
        self.assertEqual(self.packs(2)[0]['state'], 'paid')
        self.assertEqual(self.usage_of(self.other)['pack_questions_remaining'], PACK_SIZE)
        with self.sessions() as db:
            user = db.get(User, 2)
            self.assertEqual((user.plan, user.payment_provider, user.provider_subscription_id), ('free', None, None))

    def test_be7_event_before_binding_is_retryable(self):
        response = self.send_lava(self.lava_event(str(uuid4()), PACK_PRODUCT, LAVA_PACK_RUB, 'completed'))
        self.assertEqual(response.status_code, 503, response.text)
        with self.sessions() as db:
            self.assertEqual(db.query(BillingEvent).count(), 0)

    def test_be7_wrong_or_unconfirmed_invoice_read_never_grants(self):
        contract, _ = self.buy_lava_pack()
        self.lava_invoices[contract]['status'] = 'COMPLETED'
        good = copy.deepcopy(self.lava_invoices[contract])
        cases = [('amount', 400, {'receipt': {'amount': 100, 'currency': 'RUB'}}),
                 ('currency', 400, {'receipt': {'amount': LAVA_PACK_RUB, 'currency': 'USD'}}),
                 ('type', 400, {'type': 'SUBSCRIPTION'}),
                 ('email', 400, {'buyer': {'email': 'other@example.test'}}),
                 ('not completed', 503, {'status': 'IN_PROGRESS'})]
        for what, status, change in cases:
            with self.subTest(what=what):
                self.lava_invoices[contract] = {**copy.deepcopy(good), **change}
                value = self.lava_event(contract, PACK_PRODUCT, LAVA_PACK_RUB, 'completed', at=datetime.utcnow() + timedelta(seconds=len(what)))
                self.assertEqual(self.send_lava(value).status_code, status)
                self.assertEqual(self.usage_of(self.other)['pack_questions_remaining'], 0)
        self.lava_invoices[contract] = good  # positive control
        self.assertEqual(self.send_lava(self.lava_event(contract, PACK_PRODUCT, LAVA_PACK_RUB, 'completed')).status_code, 200)
        self.assertEqual(self.usage_of(self.other)['pack_questions_remaining'], PACK_SIZE)

    def test_be7_failed_payment_marks_pack_failed(self):
        contract, _ = self.buy_lava_pack()
        self.lava_invoices[contract]['status'] = 'FAILED'
        response = self.send_lava(self.lava_event(contract, PACK_PRODUCT, LAVA_PACK_RUB, 'failed', kind='payment.failed'))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.packs(2)[0]['state'], 'failed')
        self.assertEqual(self.usage_of(self.other)['pack_questions_remaining'], 0)

    def test_be7_refund_with_one_matching_pack_revokes(self):
        contract, _ = self.buy_lava_pack()
        self.grant_lava_pack(contract)
        self.assertEqual(self.send_lava(self.lava_financial()).status_code, 200)
        self.assertEqual(self.packs(2)[0]['state'], 'revoked')
        self.assertEqual(self.usage_of(self.other)['pack_questions_remaining'], 0)

    def test_be7_chargeback_with_one_matching_pack_revokes(self):
        contract, _ = self.buy_lava_pack()
        self.grant_lava_pack(contract)
        self.assertEqual(self.send_lava(self.lava_financial('chargeback.initiated')).status_code, 200)
        row = self.packs(2)[0]
        self.assertEqual((row['state'], row['revoke_reason']), ('revoked', 'dispute'))

    def test_be7_refund_with_two_matching_packs_is_unmatched(self):
        first, _ = self.buy_lava_pack(); self.grant_lava_pack(first)
        second, _ = self.buy_lava_pack(); self.grant_lava_pack(second)
        response = self.send_lava(self.lava_financial())
        self.assertEqual((response.status_code, response.json()['outcome']), (200, 'recorded_unmatched'))
        self.assertEqual([p['state'] for p in self.packs(2)], ['paid', 'paid'])
        self.assertEqual(self.usage_of(self.other)['pack_questions_remaining'], 2 * PACK_SIZE)


# =========================================================================== BE-8 — buying a pack
class PricingBE8PackCheckoutTests(PricingBase):
    def test_be8_stripe_pack_in_beta_is_409_without_provider(self):
        with patch.dict(os.environ, {FLAG: 'true'}):
            response = self.checkout('stripe', self.other, 'question_pack')
        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn(BETA_PACK, text_of(response))
        self.api.prices.retrieve.assert_not_called()
        self.api.customers.create.assert_not_called()
        self.api.checkout.sessions.create.assert_not_called()

    def test_be8_guard_monthly_checkout_in_beta_unchanged(self):
        with patch.dict(os.environ, {FLAG: 'true'}):
            self.assertEqual(self.checkout('stripe', self.other).status_code, 200)

    def test_be8_lava_pack_in_beta_is_409_without_provider(self):
        with patch.dict(os.environ, {FLAG: 'true'}):
            response = self.checkout('lava', self.other, 'question_pack')
        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn(BETA_PACK, text_of(response))
        self.network.assert_not_called()

    def test_be8_missing_pack_variable_is_503(self):
        for provider, variable in (('stripe', 'STRIPE_QUESTION_PACK_PRICE_ID'), ('lava', 'LAVA_PACK_OFFER_ID')):
            with self.subTest(provider=provider), patch.dict(os.environ):
                os.environ.pop(variable)
                response = self.checkout(provider, self.other, 'question_pack')
                self.assertEqual(response.status_code, 503, response.text)
                self.assertIn(UNAVAILABLE_OPTION, text_of(response))
        self.api.checkout.sessions.create.assert_not_called()
        self.assertEqual(self.lava_posts, [])

    def test_be8_unknown_product_is_422_without_provider(self):
        for provider in ('stripe', 'lava'):
            with self.subTest(provider=provider):
                self.assertEqual(self.checkout(provider, self.other, 'gift').status_code, 422)
        self.api.checkout.sessions.create.assert_not_called()
        self.network.assert_not_called()

    def test_be8_browser_price_and_quantity_are_never_used(self):
        response = self.checkout('stripe', self.other, 'question_pack', price='price_evil', quantity=9, amount=1)
        self.assertIn(response.status_code, (200, 422), response.text)
        for params, _ in self.created:
            self.assertEqual(params['line_items'], [{'price': PACK_PRICE, 'quantity': 1}])

    def test_be8_stripe_pack_checkout_params_and_double_click(self):
        first = self.checkout('stripe', self.other, 'question_pack')
        second = self.checkout('stripe', self.other, 'question_pack')
        self.assertEqual((first.status_code, second.status_code), (200, 200), second.text)
        self.assertEqual(first.json()['session_id'], second.json()['session_id'])
        self.assertEqual(len(self.created), 1)
        params, options = self.created[0]
        row, = self.packs(2)
        self.assertEqual((params['mode'], params['line_items']), ('payment', [{'price': PACK_PRICE, 'quantity': 1}]))
        self.assertEqual(params['metadata'], {'pack_id': row['id'], 'user_id': '2'})
        self.assertEqual(params['payment_intent_data']['metadata']['pack_id'], row['id'])
        self.assertNotIn('subscription_data', params)
        self.assertTrue(options['idempotency_key'])
        self.assertEqual((row['state'], row['session_id'], row['quantity'], row['provider']), ('ready', first.json()['session_id'], PACK_SIZE, 'stripe'))
        self.assertTrue(self.usage_of(self.other)['pack_pending'])

    def test_be8_pack_checkout_never_returns_the_open_subscription_session(self):
        self.bind_subscription()
        response = self.checkout('stripe', self.owner, 'question_pack')
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotEqual(response.json()['session_id'], sf.SESSION)
        self.assertEqual(self.created[0][0]['mode'], 'payment')

    def test_be8_lava_subscriber_buys_stripe_pack_and_keeps_subscription(self):
        self.premium(user_id=2)
        with self.sessions() as db:
            user = db.get(User, 2)
            user.payment_provider, user.provider_subscription_id, user.subscription_status = 'lava', lf.CONTRACT, 'active'
            db.commit()
        session_id, pack_id = self.buy_stripe_pack()
        self.assertEqual(self.send(self.pay_stripe_pack(session_id)).status_code, 200)
        with self.sessions() as db:
            user = db.get(User, 2)
            self.assertEqual((user.plan, user.payment_provider, user.provider_subscription_id, user.provider_customer_id,
                              user.subscription_status), ('premium', 'lava', lf.CONTRACT, None, 'active'))
        state = self.usage_of(self.other)
        self.assertEqual((state['pack_questions_remaining'], state['pack_pending']), (PACK_SIZE, False))

    def test_be8_stripe_subscriber_buys_lava_pack(self):
        self.premium(user_id=2)
        with self.sessions() as db:
            user = db.get(User, 2)
            user.payment_provider, user.provider_customer_id, user.provider_subscription_id, user.subscription_status = 'stripe', 'cus_x', 'sub_x', 'active'
            db.commit()
        contract, _ = self.buy_lava_pack()
        self.grant_lava_pack(contract)
        with self.sessions() as db:
            self.assertEqual(db.get(User, 2).payment_provider, 'stripe')
        self.assertEqual(self.usage_of(self.other)['pack_questions_remaining'], PACK_SIZE)

    def test_be8_lava_pack_double_click_reuses_link(self):
        first = self.checkout('lava', self.other, 'question_pack')
        second = self.checkout('lava', self.other, 'question_pack')
        self.assertEqual((first.status_code, second.status_code), (200, 200), second.text)
        self.assertEqual(first.json(), second.json())
        self.assertEqual([body['offerId'] for body in self.lava_posts], [PACK_OFFER])

    def test_be8_lava_pack_retry_reads_the_invoice_first(self):
        contract, url = self.buy_lava_pack()
        self.lava_invoices[contract]['status'] = 'FAILED'
        response = self.checkout('lava', self.other, 'question_pack')
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotEqual(response.json()['url'], url)
        self.assertEqual(len(self.lava_posts), 2)
        newest = [c for c, inv in self.lava_invoices.items() if inv['type'] == 'INVOICE'][-1]
        self.lava_invoices[newest]['status'] = 'COMPLETED'
        pending = self.checkout('lava', self.other, 'question_pack')
        self.assertEqual(pending.status_code, 409, pending.text)
        self.assertIn(NOT_AGAIN, text_of(pending))
        self.assertEqual(len(self.lava_posts), 2)


# =========================================================================== BE-9 — annual plan, Stripe
class PricingBE9StripeAnnualTests(PricingBase):
    def annual_subscription(self, amount_paid=STRIPE_ANNUAL_CENTS, item_price=ANNUAL_PRICE):
        self.year_end = self.start + timedelta(days=365)
        price = copy.deepcopy(self.prices[item_price])
        self.sub['items']['data'][0].update(price=price, current_period_end=sf.timestamp(self.year_end))
        invoice = self.invoices[sf.INVOICE]
        invoice['amount_paid'] = amount_paid
        line = invoice['lines']['data'][0]
        line['pricing']['price_details']['price'] = item_price
        line['period'] = {'start': sf.timestamp(self.start), 'end': sf.timestamp(self.year_end)}
        self.invoice_amounts[sf.INVOICE] = amount_paid

    def test_be9_year_in_beta_is_409_without_provider(self):
        with patch.dict(os.environ, {FLAG: 'true'}):
            response = self.checkout('stripe', self.other, 'premium_year')
        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn(BETA_YEAR, text_of(response))
        self.api.prices.retrieve.assert_not_called()
        self.api.checkout.sessions.create.assert_not_called()

    def test_be9_year_checkout_uses_the_annual_price(self):
        response = self.checkout('stripe', self.other, 'premium_year')
        self.assertEqual(response.status_code, 200, response.text)
        params, _ = self.created[0]
        self.assertEqual((params['mode'], params['line_items']), ('subscription', [{'price': ANNUAL_PRICE, 'quantity': 1}]))
        with self.engine.connect() as conn:
            self.assertEqual(conn.execute(text('SELECT price_id FROM stripe_checkouts WHERE user_id = 2')).scalars().all(), [ANNUAL_PRICE])

    def test_be9_wrong_annual_price_or_missing_variable_is_503(self):
        good = copy.deepcopy(self.prices[ANNUAL_PRICE])
        for change in ({'unit_amount': STRIPE_MONTH_CENTS}, {'unit_amount': 5999}, {'currency': 'eur'}, {'active': False},
                       {'livemode': False}, {'recurring': {'interval': 'month', 'interval_count': 1, 'usage_type': 'licensed'}},
                       {'recurring': {'interval': 'year', 'interval_count': 2, 'usage_type': 'licensed'}}):
            with self.subTest(change=change):
                self.prices[ANNUAL_PRICE] = {**copy.deepcopy(good), **change}
                self.assertEqual(self.checkout('stripe', self.other, 'premium_year').status_code, 503)
        self.prices[ANNUAL_PRICE] = good
        with patch.dict(os.environ):
            os.environ.pop('STRIPE_PREMIUM_ANNUAL_PRICE_ID')
            response = self.checkout('stripe', self.other, 'premium_year')
            self.assertEqual(response.status_code, 503)
            self.assertIn(UNAVAILABLE_OPTION, text_of(response))
        self.api.checkout.sessions.create.assert_not_called()

    def test_be9_paid_annual_invoice_grants_a_year(self):
        self.bind_subscription(price_id=ANNUAL_PRICE)
        self.annual_subscription()
        response = self.send(self.event('invoice.paid', sf.INVOICE))
        self.assertEqual((response.status_code, response.json().get('outcome')), (200, 'granted'), response.text)
        with self.sessions() as db:
            user = db.get(User, 1)
            self.assertEqual((user.plan, user.current_period_start, user.current_period_end), ('premium', self.start, self.year_end))
            self.assertEqual(db.query(BillingPeriod).one().period_end, self.year_end)
        self.assertEqual(self.interval(1), 'year')
        state = self.usage_of(self.owner)
        self.assertEqual((state['plan_interval'], state['gpt_messages_limit']), ('year', 300))

    def test_be9_monthly_price_on_annual_checkout_is_400(self):
        self.bind_subscription(price_id=ANNUAL_PRICE)
        self.annual_subscription(amount_paid=STRIPE_MONTH_CENTS, item_price=sf.PRICE)
        self.assertEqual(self.send(self.event('invoice.paid', sf.INVOICE)).status_code, 400)
        self.assertEqual(self.usage_of(self.owner)['plan'], 'free')

    def test_be9_underpaid_annual_invoice_is_400(self):
        self.bind_subscription(price_id=ANNUAL_PRICE)
        self.annual_subscription(amount_paid=STRIPE_MONTH_CENTS)
        self.assertEqual(self.send(self.event('invoice.paid', sf.INVOICE)).status_code, 400)
        self.assertEqual(self.usage_of(self.owner)['plan'], 'free')

    def test_be9_open_month_session_then_year_reads_expires_once_and_creates(self):
        month = self.checkout('stripe', self.other)
        self.assertEqual(month.status_code, 200, month.text)
        year = self.checkout('stripe', self.other, 'premium_year')
        self.assertEqual(year.status_code, 200, year.text)
        self.assertNotEqual(year.json()['session_id'], month.json()['session_id'])
        self.assertEqual(self.api.checkout.sessions.expire.call_count, 1)
        call = self.api.checkout.sessions.expire.call_args
        self.assertEqual(call.args[0], month.json()['session_id'])
        self.assertTrue(call.kwargs.get('options', {}).get('idempotency_key'))
        self.assertEqual([p['line_items'][0]['price'] for p, _ in self.created], [sf.PRICE, ANNUAL_PRICE])
        again = self.checkout('stripe', self.other, 'premium_year')
        self.assertEqual(again.json()['session_id'], year.json()['session_id'])
        self.assertEqual((self.api.checkout.sessions.expire.call_count, len(self.created)), (1, 2))

    def test_be9_guard_completed_month_session_then_year_is_409(self):
        # GUARD: today a completed session already answers 409 «Проверяем оплату»; the switch must keep it, without expire.
        month = self.checkout('stripe', self.other)
        self.remote[month.json()['session_id']]['status'] = 'complete'
        response = self.checkout('stripe', self.other, 'premium_year')
        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn('Проверяем оплату', text_of(response))
        self.api.checkout.sessions.expire.assert_not_called()
        self.assertEqual(len(self.created), 1)

    def test_be9_active_premium_cannot_start_a_year(self):
        self.premium(user_id=2)
        with self.sessions() as db:
            user = db.get(User, 2)
            user.payment_provider, user.provider_customer_id, user.provider_subscription_id, user.subscription_status = 'stripe', 'cus_x', 'sub_x', 'active'
            db.commit()
        response = self.checkout('stripe', self.other, 'premium_year')
        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn(HAS_PREMIUM_YEAR, text_of(response))
        self.api.checkout.sessions.create.assert_not_called()


# =========================================================================== BE-10 — annual plan, Lava
class PricingBE10LavaAnnualTests(PricingBase):
    def annual_contract(self):
        response = self.checkout('lava', self.other, 'premium_year')
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.lava_posts[-1]['offerId'], ANNUAL_OFFER, 'no annual invoice was created (product ignored?)')
        contract = [c for c, inv in self.lava_invoices.items() if inv['amountTotal']['amount'] == LAVA_ANNUAL_RUB][-1]
        self.year_end = self.start + timedelta(days=365)
        self.lava_subscriptions[contract] = {'id': contract, 'datetime': iso(self.start), 'status': 'COMPLETED',
            'receipt': {'amount': LAVA_ANNUAL_RUB, 'currency': 'RUB'}, 'buyer': {'email': BUYER}, 'periodicity': 'PERIOD_YEAR',
            'subscriptionStatus': 'ACTIVE', 'expiredAt': iso(self.year_end), 'cancelledAt': None, 'terminatedAt': None,
            'recurrentPayments': []}
        return contract

    def test_be10_year_in_beta_is_409_without_provider(self):
        with patch.dict(os.environ, {FLAG: 'true'}):
            response = self.checkout('lava', self.other, 'premium_year')
        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn(BETA_YEAR, text_of(response))
        self.network.assert_not_called()

    def test_be10_year_invoice_uses_the_annual_offer(self):
        self.annual_contract()
        body = self.lava_posts[-1]
        self.assertEqual((body['offerId'], body['periodicity'], body['currency']), (ANNUAL_OFFER, 'PERIOD_YEAR', 'RUB'))
        self.assertNotIn('amount', body)
        with self.engine.connect() as conn:
            self.assertEqual(conn.execute(text('SELECT periodicity FROM lava_checkouts WHERE user_id = 2')).scalars().all(), ['PERIOD_YEAR'])

    def test_be10_catalog_drift_or_missing_variable_is_503_before_invoice(self):
        offer = self.lava_catalog['items'][1]['offers'][0]
        good = copy.deepcopy(self.lava_catalog)
        for what, change in (('amount', lambda: offer['prices'][0].update(amount=3990)),
                             ('periodicity', lambda: offer['prices'][0].update(periodicity='MONTHLY')),
                             ('two prices', lambda: offer['prices'].append({'currency': 'RUB', 'amount': LAVA_ANNUAL_RUB, 'periodicity': 'PERIOD_YEAR'})),
                             ('type', lambda: self.lava_catalog['items'][1].update(type='DIGITAL_PRODUCT'))):
            with self.subTest(what=what):
                self.lava_catalog = copy.deepcopy(good)
                offer = self.lava_catalog['items'][1]['offers'][0]
                change()
                self.assertEqual(self.checkout('lava', self.other, 'premium_year').status_code, 503)
        self.lava_catalog = good
        with patch.dict(os.environ):
            os.environ.pop('LAVA_ANNUAL_OFFER_ID')
            response = self.checkout('lava', self.other, 'premium_year')
            self.assertEqual(response.status_code, 503)
            self.assertIn(UNAVAILABLE_OPTION, text_of(response))
        self.assertEqual(self.lava_posts, [])

    def test_be10_paid_annual_subscription_grants_a_year(self):
        contract = self.annual_contract()
        response = self.send_lava(self.lava_event(contract, ANNUAL_PRODUCT, LAVA_ANNUAL_RUB, 'subscription-active', at=self.start))
        self.assertEqual(response.status_code, 200, response.text)
        with self.sessions() as db:
            user = db.get(User, 2)
            self.assertEqual((user.plan, user.payment_provider, user.current_period_end), ('premium', 'lava', self.year_end))
        self.assertEqual(self.interval(2), 'year')

    def test_be10_snapshot_with_other_periodicity_is_400(self):
        contract = self.annual_contract()
        self.lava_subscriptions[contract]['periodicity'] = 'MONTHLY'
        response = self.send_lava(self.lava_event(contract, ANNUAL_PRODUCT, LAVA_ANNUAL_RUB, 'subscription-active', at=self.start))
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(self.usage_of(self.other)['plan'], 'free')

    def test_be10_renewal_amount_is_the_annual_one(self):
        contract = self.annual_contract()
        self.assertEqual(self.send_lava(self.lava_event(contract, ANNUAL_PRODUCT, LAVA_ANNUAL_RUB, 'subscription-active', at=self.start)).status_code, 200)
        wrong, renewal = str(uuid4()), str(uuid4())
        snapshot = self.lava_subscriptions[contract]
        snapshot['expiredAt'] = iso(self.year_end + timedelta(days=365))
        snapshot['recurrentPayments'] = [{'id': wrong, 'status': 'COMPLETED', 'datetime': iso(self.year_end), 'amount': LAVA_MONTH_RUB, 'currency': 'RUB'}]
        monthly = self.lava_event(wrong, ANNUAL_PRODUCT, LAVA_MONTH_RUB, 'subscription-active',
                                  kind='subscription.recurring.payment.success', at=self.year_end, parent=contract)
        self.assertEqual(self.send_lava(monthly).status_code, 400)
        snapshot['recurrentPayments'] = [{'id': renewal, 'status': 'COMPLETED', 'datetime': iso(self.year_end), 'amount': LAVA_ANNUAL_RUB, 'currency': 'RUB'}]
        annual = self.lava_event(renewal, ANNUAL_PRODUCT, LAVA_ANNUAL_RUB, 'subscription-active',
                                 kind='subscription.recurring.payment.success', at=self.year_end, parent=contract)
        self.assertEqual(self.send_lava(annual).status_code, 200)
        with self.sessions() as db:
            self.assertEqual(db.get(User, 2).current_period_end, self.year_end + timedelta(days=365))

    def test_be10_open_monthly_invoice_then_year_is_409(self):
        month = self.checkout('lava', self.other)
        self.assertEqual(month.status_code, 200, month.text)
        response = self.checkout('lava', self.other, 'premium_year')
        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn(LAVA_OTHER_INVOICE, text_of(response))
        self.assertEqual(len(self.lava_posts), 1)


# =========================================================================== BE-11 — annual quota, variant A (300 in each month)
class PricingBE11AnnualQuotaTests(PricingBase):
    def year_user(self, anchor, end=None):
        self.anchor, self.year_end = anchor, end or anchor.replace(year=anchor.year + 1)
        with self.sessions() as db:
            usage.set_plan(db, 1, 'premium', self.anchor, self.year_end)
        self.set_interval(1, 'year')
        self.chart_for(1)

    def seed_year(self, count, created_at):
        self.seed_usage(count, plan='premium', start=self.anchor, end=self.year_end, created_at=created_at)

    def at(self, moment):
        return patch('modules.usage.utcnow', return_value=moment)

    def state_at(self, moment):
        with self.at(moment), self.sessions() as db:
            return usage.account_usage(db, 1)

    def test_be11_window_boundary_resets_to_300(self):
        self.year_user(datetime(2027, 1, 31, 10, 0, 0))
        self.seed_year(300, datetime(2027, 2, 1, 12, 0, 0))
        before = self.state_at(datetime(2027, 2, 28, 9, 59, 59))
        self.assertEqual((before['gpt_messages_used'], before['gpt_messages_available']), (300, 0))
        self.assertEqual(parse_dt(before['gpt_messages_reset_at']), datetime(2027, 2, 28, 10, 0, 0))
        with self.at(datetime(2027, 2, 28, 9, 59, 59)):
            self.assertEqual(self.refused()['code'], 'GPT_LIMIT_REACHED')
        after = self.state_at(datetime(2027, 2, 28, 10, 0, 0))
        self.assertEqual((after['gpt_messages_used'], after['gpt_messages_available'], after['gpt_messages_limit']), (0, 300, 300))
        with self.at(datetime(2027, 2, 28, 10, 0, 0)):
            self.spend()

    def test_be11_windows_always_count_from_the_31st_anchor(self):
        self.year_user(datetime(2027, 1, 31, 10, 0, 0))
        self.seed_year(300, datetime(2027, 3, 1, 12, 0, 0))  # window 2: 28 Feb 10:00 .. 31 Mar 10:00
        self.assertEqual(self.state_at(datetime(2027, 3, 28, 10, 0, 0))['gpt_messages_available'], 0)  # not chained from 28 Feb
        self.assertEqual(self.state_at(datetime(2027, 3, 31, 9, 59, 59))['gpt_messages_available'], 0)
        self.assertEqual(self.state_at(datetime(2027, 3, 31, 10, 0, 0))['gpt_messages_available'], 300)

    def test_be11_leap_year_window_ends_on_29_february(self):
        self.year_user(datetime(2028, 1, 31, 10, 0, 0))
        self.seed_year(300, datetime(2028, 2, 10, 0, 0, 0))
        state = self.state_at(datetime(2028, 2, 29, 9, 59, 59))
        self.assertEqual(state['gpt_messages_available'], 0)
        self.assertEqual(parse_dt(state['gpt_messages_reset_at']), datetime(2028, 2, 29, 10, 0, 0))
        self.assertEqual(self.state_at(datetime(2028, 2, 29, 10, 0, 0))['gpt_messages_available'], 300)

    def test_be11_reservation_before_boundary_counts_in_its_own_window(self):
        self.year_user(datetime(2027, 1, 31, 10, 0, 0))
        self.seed_year(299, datetime(2027, 2, 1, 12, 0, 0))
        with self.at(datetime(2027, 2, 28, 9, 59, 59)):
            reservation = self.reserve()
        with self.at(datetime(2027, 2, 28, 10, 0, 1)):
            self.finalize(reservation)
        self.assertEqual(self.state_at(datetime(2027, 2, 28, 10, 0, 1))['gpt_messages_available'], 300)
        self.assertEqual(self.state_at(datetime(2027, 2, 28, 9, 59, 59))['gpt_messages_used'], 300)

    def test_be11_remainder_shorter_than_a_month_joins_the_last_window(self):
        self.year_user(datetime(2027, 1, 31, 10, 0, 0), end=datetime(2028, 2, 2, 10, 0, 0))
        self.seed_year(300, datetime(2028, 1, 5, 0, 0, 0))  # window 12: 31 Dec 2027 10:00 .. 2 Feb 2028 10:00
        self.assertEqual(self.state_at(datetime(2028, 2, 1, 0, 0, 0))['gpt_messages_available'], 0)

    def test_be11_rows_keep_the_year_start_as_period_identity(self):
        self.year_user(datetime(2027, 1, 31, 10, 0, 0))
        with self.at(datetime(2027, 4, 15, 0, 0, 0)):
            self.spend()
        with self.sessions() as db:
            row = db.query(GPTUsage).filter_by(user_id=1, status='succeeded').one()
            self.assertEqual((row.period_start, row.period_end), (self.anchor, self.year_end))

    def test_be11_month_interval_is_300_per_period(self):
        self.premium()
        self.set_interval(1, 'month')
        self.seed_usage(300, plan='premium', start=self.pstart, end=self.pend, created_at=self.pstart)
        state = self.usage_of(self.owner)
        self.assertEqual((state['gpt_messages_available'], state['plan_interval']), (0, 'month'))
        self.assertEqual(parse_dt(state['gpt_messages_reset_at']), self.pend)

    def test_be11_guard_null_interval_is_300_per_period(self):
        # GUARD: manual Premium and rows before renewal (interval NULL) keep today's 300 per paid period.
        now = datetime.utcnow()
        self.premium(start=now - timedelta(days=40), end=now + timedelta(days=20))
        self.seed_usage(150, plan='premium', start=self.pstart, end=self.pend, created_at=now - timedelta(days=39))
        self.seed_usage(150, plan='premium', start=self.pstart, end=self.pend, created_at=now - timedelta(days=1))
        state = self.usage_of(self.owner)
        self.assertEqual((state['gpt_messages_used'], state['gpt_messages_available'], state['gpt_messages_limit']), (300, 0, 300))


# =========================================================================== BE-12 — /account/usage and /public-config
class PricingBE12AccountFieldsTests(PricingBase):
    FLAGS = ('stripe_annual_checkout_available', 'lava_annual_checkout_available',
             'stripe_pack_checkout_available', 'lava_pack_checkout_available')

    def test_be12_free_account_fields(self):
        state = self.usage_of(self.other)
        self.assertEqual({k: state[k] for k in ('plan_interval', 'gpt_messages_reset_at', 'pack_questions_remaining', 'pack_pending')},
                         {'plan_interval': None, 'gpt_messages_reset_at': None, 'pack_questions_remaining': 0, 'pack_pending': False})
        self.assertEqual([state[k] for k in self.FLAGS], [True] * 4)
        self.assertEqual(state['gpt_messages_available'], 10)

    def test_be12_pack_remainder_is_separate_from_plan_availability(self):
        self.seed_pack(user_id=2)
        state = self.usage_of(self.other)
        self.assertEqual((state['gpt_messages_available'], state['pack_questions_remaining']), (10, PACK_SIZE))

    def test_be12_flags_are_off_in_beta(self):
        with patch.dict(os.environ, {FLAG: 'true'}):
            state = self.usage_of(self.other)
        self.assertEqual([state[k] for k in self.FLAGS], [False] * 4)

    def test_be12_each_flag_follows_its_variable(self):
        for variable, flag, bad in (('STRIPE_PREMIUM_ANNUAL_PRICE_ID', 'stripe_annual_checkout_available', 'not-a-price'),
                                    ('STRIPE_QUESTION_PACK_PRICE_ID', 'stripe_pack_checkout_available', 'price_bad value'),
                                    ('LAVA_ANNUAL_OFFER_ID', 'lava_annual_checkout_available', 'not-a-uuid'),
                                    ('LAVA_PACK_OFFER_ID', 'lava_pack_checkout_available', '')):
            with self.subTest(variable=variable):
                with patch.dict(os.environ):
                    os.environ.pop(variable)
                    state = self.usage_of(self.other)
                    self.assertFalse(state[flag])
                    self.assertEqual(sum(state[k] for k in self.FLAGS), 3)
                with patch.dict(os.environ, {variable: bad}):
                    self.assertFalse(self.usage_of(self.other)[flag])

    def test_be12_pack_pending_until_the_server_grants(self):
        session_id, _ = self.buy_stripe_pack()
        self.assertEqual((self.usage_of(self.other)['pack_pending'], self.usage_of(self.other)['pack_questions_remaining']), (True, 0))
        self.assertEqual(self.send(self.pay_stripe_pack(session_id)).status_code, 200)
        state = self.usage_of(self.other)
        self.assertEqual((state['pack_pending'], state['pack_questions_remaining']), (False, PACK_SIZE))

    def test_be12_public_config_offers(self):
        body = self.client.get('/public-config').json()
        self.assertEqual(body['free_beta_enabled'], False)
        self.assertEqual(body['offers'], {'premium_year': True, 'question_pack': True, 'pack_size': PACK_SIZE})
        with patch.dict(os.environ, {FLAG: 'true'}):
            beta = self.client.get('/public-config').json()
        self.assertEqual((beta['free_beta_enabled'], beta['offers']), (True, {'premium_year': False, 'question_pack': False, 'pack_size': PACK_SIZE}))
        # Coordinator clarification (DESIGN §12): true when configured for at least one provider and beta is off.
        with patch.dict(os.environ):
            os.environ.pop('STRIPE_PREMIUM_ANNUAL_PRICE_ID')
            os.environ.pop('LAVA_PACK_OFFER_ID')
            one = self.client.get('/public-config').json()['offers']
            self.assertEqual(one, {'premium_year': True, 'question_pack': True, 'pack_size': PACK_SIZE})
            os.environ.pop('LAVA_ANNUAL_OFFER_ID')
            os.environ.pop('STRIPE_QUESTION_PACK_PRICE_ID')
            none = self.client.get('/public-config').json()['offers']
            self.assertEqual(none, {'premium_year': False, 'question_pack': False, 'pack_size': PACK_SIZE})
        self.assertNotIn('4.99', json.dumps(body))  # prices are not exposed by the API
        self.assertNotIn('399', json.dumps(body))


# =========================================================================== BE-13 — analytics: closed values (schema)
class PricingBE13SchemaTests(unittest.TestCase):
    def test_be13_new_server_events_and_values_are_closed(self):
        check = schema.checked_properties
        self.assertEqual(check('question_pack_purchased', {'provider': 'stripe'}, source='server'), {'provider': 'stripe'})
        self.assertEqual(check('annual_plan_started', {'provider': 'lava'}, source='server'), {'provider': 'lava'})
        for product in ('premium_month', 'premium_year', 'question_pack'):
            self.assertEqual(check('checkout_initiated', {'provider': 'stripe', 'product': product}, source='server'),
                             {'provider': 'stripe', 'product': product})
        refused = [('checkout_initiated', {'provider': 'stripe'}, 'server'),
                   ('checkout_initiated', {'provider': 'stripe', 'product': 'gift'}, 'server'),
                   ('checkout_initiated', {'provider': 'stripe', 'product': 4.99}, 'server'),
                   ('question_pack_purchased', {'provider': 'paypal'}, 'server'),
                   ('question_pack_purchased', {'provider': 'stripe', 'amount': '499'}, 'server'),
                   ('question_pack_purchased', {'provider': 'stripe'}, 'client'),
                   ('annual_plan_started', {'provider': 'stripe'}, 'client')]
        for name, properties, source in refused:
            with self.subTest(name=name, properties=properties, source=source), self.assertRaises(ValueError):
                check(name, properties, source=source)

    def test_be13_new_client_events_and_values_are_closed(self):
        check = schema.checked_properties
        for moment in ('last_free_question', 'question_limit', 'chart_limit'):
            self.assertEqual(check('offer_moment_shown', {'moment': moment}, source='client'), {'moment': moment})
        for option in ('premium_month', 'premium_year', 'question_pack'):
            self.assertEqual(check('offer_option_selected', {'option': option}, source='client'), {'option': option})
        for name, properties in (('offer_moment_shown', {'moment': 'pricing_page'}), ('offer_option_selected', {'option': 'gift'}),
                                 ('offer_moment_shown', {}), ('offer_moment_shown', {'moment': 'question_limit', 'plan': 'free'})):
            with self.subTest(name=name, properties=properties), self.assertRaises(ValueError):
                check(name, properties, source='client')
        self.assertEqual(check('payment_provider_selected', {'provider': 'lava'}, source='client'), {'provider': 'lava'})  # unchanged


# =========================================================================== Round 2 (REVIEW.md P2-1, P2-2; DESIGN §12 last two bullets)
class PricingR2PackPendingTests(PricingBase):
    """P2-1: pack_pending only while a pack in creating/ready was created at most 30 minutes ago (server clock
    modules.usage.utcnow). Old unfinished packs stay records but never hold the flag."""

    def pending_at(self, moment, headers=None):
        with patch('modules.usage.utcnow', return_value=moment):
            return self.usage_of(headers or self.other)['pack_pending']

    def test_r2_p2_1_window_is_30_minutes_from_creation(self):
        created = datetime.utcnow().replace(microsecond=0) - timedelta(hours=1)
        self.seed_pack(user_id=2, state='ready', created_at=created)
        self.assertTrue(self.pending_at(created + timedelta(minutes=29, seconds=59)))
        self.assertTrue(self.pending_at(created + timedelta(minutes=30)))
        self.assertFalse(self.pending_at(created + timedelta(minutes=30, seconds=1)))

    def test_r2_p2_1_abandoned_lava_invoice_stops_holding_the_flag(self):
        self.buy_lava_pack()
        now = datetime.utcnow()
        self.assertTrue(self.pending_at(now + timedelta(minutes=1)))
        self.assertFalse(self.pending_at(now + timedelta(minutes=31)))
        self.assertFalse(self.pending_at(now + timedelta(days=3)))
        self.assertEqual(self.packs(2)[0]['state'], 'ready')  # stays a record; only FAILED / expiry change it

    def test_r2_p2_1_unfinished_stripe_pack_older_than_30_minutes_is_not_pending(self):
        self.buy_stripe_pack()  # the session expires in 1 h: the flag must not follow expires_at
        self.assertFalse(self.pending_at(datetime.utcnow() + timedelta(minutes=31)))
        self.seed_pack(user_id=2, state='creating', created_at=datetime.utcnow() - timedelta(hours=2))  # ambiguous creation, no expiry
        self.assertFalse(self.pending_at(datetime.utcnow() + timedelta(minutes=31)))

    def test_r2_p2_1_fresh_pack_still_pending_next_to_a_stale_one(self):
        self.seed_pack(user_id=2, state='ready', created_at=datetime.utcnow() - timedelta(days=2))
        self.assertFalse(self.usage_of(self.other)['pack_pending'])
        self.buy_lava_pack()  # a fresh attempt next to the stale record
        self.assertTrue(self.usage_of(self.other)['pack_pending'])


class PricingR2LavaRefundMatchingTests(PricingBase):
    """P2-2: candidates = this email's packs in paid or revoked with paid_at <= event time. One paid -> revoked;
    one already revoked -> unchanged; zero or >= 2 -> recorded_unmatched; another paid pack is never revoked."""

    def set_paid_at(self, contract, moment):
        packs = self.table('question_packs')
        with self.engine.begin() as conn:
            conn.execute(packs.update().where(packs.c.contract_id == contract).values(
                paid_at=moment, created_at=moment - timedelta(minutes=5)))

    def state_of(self, contract):
        row, = [p for p in self.packs(2) if p['contract_id'] == contract]
        return row['state'], row['revoke_reason']

    def revoked_a_then_paid_b(self):
        now = datetime.utcnow().replace(microsecond=0)
        a, _ = self.buy_lava_pack(); self.grant_lava_pack(a); self.set_paid_at(a, now - timedelta(hours=3))
        first = self.send_lava(self.lava_financial(at=now - timedelta(hours=2), partial=True, amount=100))
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(self.state_of(a), ('revoked', 'refund'))
        b, _ = self.buy_lava_pack(); self.grant_lava_pack(b); self.set_paid_at(b, now - timedelta(hours=1))
        return a, b, now

    def test_r2_p2_2_second_partial_refund_about_a_never_revokes_b(self):
        a, b, now = self.revoked_a_then_paid_b()
        response = self.send_lava(self.lava_financial(at=now, partial=True, amount=100))
        self.assertEqual((response.status_code, response.json()['outcome']), (200, 'recorded_unmatched'), response.text)
        self.assertEqual(self.state_of(b), ('paid', None))
        self.assertEqual(self.state_of(a), ('revoked', 'refund'))
        self.assertEqual(self.usage_of(self.other)['pack_questions_remaining'], PACK_SIZE)

    def test_r2_p2_2_chargeback_after_refund_never_revokes_b(self):
        a, b, now = self.revoked_a_then_paid_b()
        response = self.send_lava(self.lava_financial('chargeback.initiated', at=now))
        self.assertEqual((response.status_code, response.json()['outcome']), (200, 'recorded_unmatched'), response.text)
        self.assertEqual(self.state_of(b), ('paid', None))
        self.assertEqual(self.usage_of(self.other)['pack_questions_remaining'], PACK_SIZE)

    def test_r2_p2_2_guard_single_revoked_candidate_is_unchanged(self):
        # GUARD on 73d6bbc (it already leaves A alone); pins «уже revoked -> без изменений», the reason is kept.
        now = datetime.utcnow().replace(microsecond=0)
        a, _ = self.buy_lava_pack(); self.grant_lava_pack(a); self.set_paid_at(a, now - timedelta(hours=3))
        self.assertEqual(self.send_lava(self.lava_financial(at=now - timedelta(hours=2), partial=True, amount=100)).status_code, 200)
        for value in (self.lava_financial(at=now - timedelta(hours=1), partial=True, amount=100),
                      self.lava_financial('chargeback.initiated', at=now)):
            with self.subTest(kind=value['event_type']):
                self.assertEqual(self.send_lava(value).status_code, 200)
                self.assertEqual(self.state_of(a), ('revoked', 'refund'))

    def test_r2_p2_2_guard_zero_candidates_and_other_email_are_unmatched(self):
        # GUARD on 73d6bbc: a pack paid after the event, or another buyer's pack, is never a candidate.
        now = datetime.utcnow().replace(microsecond=0)
        a, _ = self.buy_lava_pack(); self.grant_lava_pack(a); self.set_paid_at(a, now - timedelta(hours=1))
        for value in (self.lava_financial(at=now - timedelta(hours=2)), self.lava_financial(email='a@example.test', at=now)):
            with self.subTest(email=value['data']['customer_email']):
                response = self.send_lava(value)
                self.assertEqual((response.status_code, response.json()['outcome']), (200, 'recorded_unmatched'))
                self.assertEqual(self.state_of(a), ('paid', None))

# =========================================================================== Round 3 (REVIEW.md «Раунд 2» P2-3; DESIGN §13)
from urllib.parse import parse_qs, urlsplit  # noqa: E402

from modules import lava_client  # noqa: E402

LAVA_RETURN_FIELDS = ('successful_return_url', 'failure_return_url', 'cancel_return_url')


class PricingR3ReturnUrlTests(PricingBase):
    """BE-R3-1..3, BE-R3-5: every return URL the server builds carries exactly one closed `product=` (offers.PRODUCTS);
    the rest of the variable stays as is (literal {CHECKOUT_SESSION_ID}, billing, status), the parameter goes before `#`."""

    def query(self, url):
        return parse_qs(urlsplit(url).query, keep_blank_values=True)

    def assert_product(self, url, product, provider):
        self.assertEqual(url.count('product='), 1, url)
        query = self.query(url)
        self.assertEqual(query.get('product'), [product], url)
        self.assertEqual(query.get('billing'), [provider], url)
        if provider == 'stripe':
            self.assertIn('session_id={CHECKOUT_SESSION_ID}', url)  # Stripe's placeholder, not percent-encoded

    def stripe_return(self, index):
        return self.created[index][0]['return_url']

    def test_be_r3_1_stripe_return_url_names_the_product(self):
        self.buy_stripe_pack()
        self.assert_product(self.stripe_return(0), 'question_pack', 'stripe')
        self.assertEqual(self.checkout('stripe', self.owner).status_code, 200)
        self.assert_product(self.stripe_return(1), 'premium_month', 'stripe')
        with self.sessions() as db:  # a fresh account for the year (user 1 already has a monthly attempt)
            db.add(User(id=3, email='c@example.test', password_hash='unused')); db.commit()
        third = {'Authorization': 'Bearer ' + fixtures.create_access_token({'sub': '3'})}
        self.assertEqual(self.checkout('stripe', third, 'premium_year').status_code, 200)
        self.assert_product(self.stripe_return(2), 'premium_year', 'stripe')

    def test_be_r3_2_lava_return_urls_name_the_product(self):
        self.buy_lava_pack()
        self.assertEqual(self.checkout('lava', self.owner).status_code, 200)
        with self.sessions() as db:
            db.add(User(id=3, email='c@example.test', password_hash='unused')); db.commit()
        third = {'Authorization': 'Bearer ' + fixtures.create_access_token({'sub': '3'})}
        self.assertEqual(self.checkout('lava', third, 'premium_year').status_code, 200)
        for body, product in zip(self.lava_posts, ('question_pack', 'premium_month', 'premium_year')):
            for field in LAVA_RETURN_FIELDS:
                with self.subTest(product=product, field=field):
                    self.assert_product(body[field], product, 'lava')

    def test_be_r3_3_fragment_keeps_its_place(self):
        with patch.dict(os.environ, {'STRIPE_CHECKOUT_RETURN_URL': sf.ENV['STRIPE_CHECKOUT_RETURN_URL'] + '#paid',
                                     'LAVA_SUCCESS_RETURN_URL': lf.ENV['LAVA_SUCCESS_RETURN_URL'] + '&status=success#done'}):
            self.buy_stripe_pack()
            self.buy_lava_pack()
        stripe_url = self.stripe_return(0)
        self.assertTrue(stripe_url.endswith('#paid'), stripe_url)
        self.assertEqual(urlsplit(stripe_url).fragment, 'paid')
        self.assert_product(stripe_url, 'question_pack', 'stripe')
        lava_url = self.lava_posts[0]['successful_return_url']
        self.assertEqual(urlsplit(lava_url).fragment, 'done')
        self.assertEqual(self.query(lava_url).get('status'), ['success'])
        self.assert_product(lava_url, 'question_pack', 'lava')

    def test_be_r3_3_variable_with_product_is_a_configuration_error(self):
        with patch.dict(os.environ, {'STRIPE_CHECKOUT_RETURN_URL': sf.ENV['STRIPE_CHECKOUT_RETURN_URL'] + '&product=premium_month'}):
            for product in (None, 'question_pack'):
                with self.subTest(provider='stripe', product=product):
                    response = self.checkout('stripe', self.other, product)
                    self.assertEqual(response.status_code, 503, response.text)
                    self.assertEqual(response.json()['detail'], stripe_billing.STRIPE_UNAVAILABLE)
        self.api.customers.create.assert_not_called()
        self.api.checkout.sessions.create.assert_not_called()
        for variable in ('LAVA_SUCCESS_RETURN_URL', 'LAVA_FAILURE_RETURN_URL', 'LAVA_CANCEL_RETURN_URL'):
            with self.subTest(provider='lava', variable=variable), patch.dict(os.environ, {variable: lf.ENV[variable] + '&product=question_pack'}):
                response = self.checkout('lava', self.other, 'question_pack')
                self.assertEqual(response.status_code, 503, response.text)
                self.assertEqual(response.json()['detail'], lava_client.RUB_NOT_AVAILABLE)
        self.network.assert_not_called()
        self.assertEqual(self.checkout('lava', self.other, 'question_pack').status_code, 200)  # positive control

    def test_be_r3_5_guard_closed_product_and_no_other_parameters(self):
        # GUARD on 5051fae: unknown product -> 422 before any provider call; extra fields ignored;
        # the server adds nothing to the return URLs except (after round 3) `product`.
        for provider in ('stripe', 'lava'):
            with self.subTest(provider=provider):
                self.assertEqual(self.checkout(provider, self.other, 'Question_Pack').status_code, 422)
        self.api.checkout.sessions.create.assert_not_called()
        self.network.assert_not_called()
        response = self.checkout('stripe', self.other, 'question_pack', price='price_evil', user_id=1)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.created[0][0]['line_items'], [{'price': PACK_PRICE, 'quantity': 1}])
        self.assertEqual(self.checkout('lava', self.other, 'question_pack', offerId='evil', amount=1).status_code, 200)
        env_keys = set(self.query(sf.ENV['STRIPE_CHECKOUT_RETURN_URL']))
        self.assertLessEqual(set(self.query(self.stripe_return(0))), env_keys | {'product'})
        for field in LAVA_RETURN_FIELDS:
            self.assertLessEqual(set(self.query(self.lava_posts[0][field])), {'billing', 'product'})


class PricingR3PackRecentlyPaidTests(PricingBase):
    """BE-R3-4: /account/usage `pack_recently_paid` — an own pack in `paid` with paid_at >= now - 30 min on the server
    clock modules.usage.utcnow; revoked, unpaid and another user's packs do not count."""

    def recent_at(self, moment, headers=None):
        with patch('modules.usage.utcnow', return_value=moment):
            return self.usage_of(headers or self.other)['pack_recently_paid']

    def test_be_r3_4_window_is_30_minutes_from_payment(self):
        paid = datetime.utcnow().replace(microsecond=0) - timedelta(hours=2)
        self.seed_pack(user_id=2, paid_at=paid)
        self.assertTrue(self.recent_at(paid + timedelta(minutes=29, seconds=59)))
        self.assertFalse(self.recent_at(paid + timedelta(minutes=30, seconds=1)))

    def test_be_r3_4_revoked_unpaid_and_foreign_packs_do_not_count(self):
        now = datetime.utcnow().replace(microsecond=0)
        self.seed_pack(user_id=2, state='revoked', paid_at=now - timedelta(minutes=1))
        self.seed_pack(user_id=2, state='creating', created_at=now - timedelta(minutes=1))
        self.seed_pack(user_id=2, state='ready', created_at=now - timedelta(minutes=1))
        self.seed_pack(user_id=1, paid_at=now - timedelta(minutes=1))
        self.assertFalse(self.recent_at(now))
        self.assertTrue(self.recent_at(now, self.owner))  # positive control: the owner of the fresh paid pack

    def test_be_r3_4_real_grant_sets_it_and_pack_pending_is_unchanged(self):
        session_id, _ = self.buy_stripe_pack()
        state = self.usage_of(self.other)
        self.assertEqual((state['pack_pending'], state['pack_recently_paid']), (True, False))
        self.assertEqual(self.send(self.pay_stripe_pack(session_id)).status_code, 200)
        state = self.usage_of(self.other)
        self.assertEqual((state['pack_pending'], state['pack_recently_paid'], state['pack_questions_remaining']), (False, True, PACK_SIZE))

import test_metrics2 as m2  # noqa: E402  (after the SQLite cases; reuses the METRICS-2 outbox harness)


class PricingBE13OutboxTests(m2.Metrics2Base):
    """Real outbox path (consent -> business commit -> fact -> outbox), as in test_metrics2."""
    send = PricingBase.send
    event = PricingBase.event
    checkout = PricingBase.checkout
    buy_stripe_pack = PricingBase.buy_stripe_pack
    pay_stripe_pack = PricingBase.pay_stripe_pack
    packs = PricingBase.packs
    table = PricingBase.table

    def seed(self):
        env = patch.dict(os.environ, ENV); env.start(); self.addCleanup(env.stop)
        install_fakes(self, True)

    def headers(self):
        return {**self.tracked('pricing'), **self.owner}

    def assert_private_free(self, rows):
        for row in rows:
            serialized = json.dumps(row['properties'])
            for canary in ('@', 'cs_', 'pi_', 'price_', '499', '4.99', '6999', '399', '4790', sf.CUSTOMER):
                self.assertNotIn(canary, serialized)

    def test_be13_checkout_initiated_carries_the_product(self):
        self.choose(owner=self.owner)
        self.assertEqual(self.checkout('stripe', self.headers(), 'question_pack').status_code, 200)
        self.assertEqual(self.checkout('stripe', self.headers()).status_code, 200)
        self.assertEqual(self.checkout('lava', self.headers(), 'premium_year').status_code, 200)
        rows = self.events('checkout_initiated')
        self.assertEqual(sorted((r['properties']['provider'], r['properties']['product']) for r in rows),
                         [('lava', 'premium_year'), ('stripe', 'premium_month'), ('stripe', 'question_pack')])
        self.assert_private_free(rows)

    def test_be13_question_pack_purchased_once_per_pack_after_commit(self):
        self.choose(owner=self.owner)
        session_id, _ = self.buy_stripe_pack(self.headers())
        value = self.pay_stripe_pack(session_id)
        self.intents['pi_pack']['status'] = 'processing'
        self.assertEqual(self.send(value).status_code, 503)  # rolled back: no fact
        self.assertEqual(self.events('question_pack_purchased'), [])
        self.intents['pi_pack']['status'] = 'succeeded'
        self.assertEqual(self.send(value).status_code, 200)
        self.assertEqual(self.send(value).json()['outcome'], 'duplicate')
        self.assertEqual(self.send(self.event('checkout.session.async_payment_succeeded', session_id)).status_code, 200)
        rows = self.events('question_pack_purchased')
        self.assertEqual([self.core(r) for r in rows], [{'provider': 'stripe'}])
        self.assertEqual(rows[0]['user_id'], 1)
        self.assert_private_free(rows)


if __name__ == '__main__':
    unittest.main()
