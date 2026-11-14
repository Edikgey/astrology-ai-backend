"""METRICS-2 regression B2 (collector), B4-B14 (DESIGN §1, §4, §6, §8).

Real API, real billing modules and real analytics transactions on an isolated SQLite database.
Fakes only: PostHog delivery is never called, Stripe/Lava answers come from the existing test
fixtures, OpenAI is a mock. No .env, no .secrets, no real keys, no environment dumps.

Facts reach the outbox only when a test drains the in-memory queue (analytics.drain_fact_queue),
so the order "business commit -> fact queued -> consent check -> outbox row" is explicit.
Negative cases end with a positive control, so an absent event is never a vacuous pass.
"""
import json
import os
import queue
import re
import sys
import unittest
from contextlib import ExitStack, contextmanager
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import event as sa_event
from sqlalchemy.exc import OperationalError

import test_my_charts as fixtures
import test_stripe as stripe_fx
import test_lava as lava_fx
import test_google_auth as google_fx
from test_product_analytics import ENV as POSTHOG_ENV
from api.auth import get_password_hash
from database.queries import User, NatalChart, GPTUsage, BillingEvent, BillingPeriod, BillingCustomer, StripeCheckout, LavaCheckout
from modules import product_analytics as analytics, stripe_billing, lava_billing, lava_client
from modules.product_analytics import AnalyticsOutbox, AnalyticsAccountPreference

FLAG = 'LUNARIA_FREE_BETA_ENABLED'
PASSWORD = 'Test-password1!'
OWNER_EMAIL = 'metrics-owner@example.com'
ANALYTICS_SESSION = '11111111-1111-4111-8111-111111111111'
# Envelope keys enqueue() adds to every event (product_analytics.enqueue); nothing else may appear.
STANDARD = {'viewport_class', 'campaign_source', 'route_key', '$session_id', 'schema_version', 'source',
            'environment', '$geoip_disable', '$ip', '$process_person_profile'}
SCHEMAS = {'guest_chart_saved': {'method'}, 'payment_failed': {'provider', 'stage'},
           'free_limit_would_block': {'resource'}}


@contextmanager
def everywhere(name, exclude=(), **kwargs):
    """Patch an analytics function in every loaded module bound to it (also `from ... import name`)."""
    target = getattr(analytics, name)
    with ExitStack() as stack:
        for module_name, module in list(sys.modules.items()):
            if module is None or module_name in exclude:
                continue
            try:
                bound = vars(module).get(name) is target
            except TypeError:
                continue
            if bound:
                stack.enter_context(patch.object(module, name, **kwargs))
        yield


def full_queue():
    value = queue.Queue(maxsize=1)
    value.put_nowait(None)
    return value


class Metrics2Base(unittest.TestCase):
    chart = fixtures.MyChartsTests.chart
    mock_calculations = fixtures.MyChartsTests.mock_calculations
    verification_code = fixtures.MyChartsTests.verification_code

    def setUp(self):
        off = patch.dict(os.environ, {'POSTHOG_ENABLED': 'false', FLAG: ''}); off.start(); self.addCleanup(off.stop)
        fixtures.MyChartsTests.setUp(self)
        self.addCleanup(fixtures.MyChartsTests.tearDown, self)
        self.seed()  # fixtures are written with analytics off: no facts from setup data
        env = patch.dict(os.environ, POSTHOG_ENV); env.start(); self.addCleanup(env.stop)
        analytics._rates.clear()
        self.clear_facts(); self.addCleanup(self.clear_facts)
        self.visitor, self.revision = map(str, (uuid4(), uuid4()))
        # Fixed id: a random UUID could contain a canary digit run ('999') by chance (B14).
        self.analytics_session = ANALYTICS_SESSION
        self.origin = {'Origin': 'https://mylunariaai.com'}

    def seed(self):
        pass

    @staticmethod
    def clear_facts():
        while not analytics._facts.empty():
            analytics._facts.get_nowait(); analytics._facts.task_done()

    def quiet(self):
        return patch.dict(os.environ, {'POSTHOG_ENABLED': 'false'})

    def beta(self, enabled=True):
        return patch.dict(os.environ, {FLAG: 'true' if enabled else 'false'})

    def choose(self, owner=None, action='accept', visitor=None, revision=None):
        response = self.client.post('/analytics/consent', headers={**self.origin, **(owner or {})}, json={
            'policy_version': 'posthog-v1', 'visitor_id': visitor or self.visitor,
            'revision': revision or self.revision, 'action': action})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def tracked(self, page='chart_creation', visitor=None, revision=None):
        return {**self.origin, 'X-Lunaria-Analytics-Visitor': visitor or self.visitor,
                'X-Lunaria-Analytics-Revision': revision or self.revision,
                'X-Lunaria-Analytics-Session': self.analytics_session, 'X-Lunaria-Analytics-Page': page}

    def drain(self):
        analytics.drain_fact_queue(limit=1000)

    def events(self, name, drain=True):
        if drain:
            self.drain()
        with self.sessions() as db:
            return [dict(user_id=row.user_id, distinct_id=row.distinct_id, properties=dict(row.properties))
                    for row in db.query(AnalyticsOutbox).filter_by(event_name=name).order_by(AnalyticsOutbox.created_at)]

    @staticmethod
    def core(row):
        """Only the event's own closed properties; the standard envelope is checked in B14."""
        return {key: row['properties'][key] for key in sorted(row['properties']) if key not in STANDARD}

    def assert_closed(self, rows, name, canaries=()):
        self.assertTrue(rows, f'no {name} row to inspect')
        for row in rows:
            with self.subTest(name=name, row=row['properties']):
                self.assertLessEqual(set(row['properties']), SCHEMAS[name] | STANDARD)
                self.assertTrue(SCHEMAS[name] <= set(row['properties']))
                self.assertIsNone(row['properties']['$ip'])
                serialized = json.dumps(row['properties'])
                for canary in ('CANARY', '@', *canaries):
                    self.assertNotIn(canary, serialized)

    def set_password(self, user_id=1, email=OWNER_EMAIL):
        with self.quiet(), self.sessions() as db:
            user = db.get(User, user_id)
            user.email, user.password_hash = email, get_password_hash(PASSWORD)
            db.commit()

    def login(self, chart_id=None, headers=None, email=OWNER_EMAIL):
        body = {'email': email, 'password': PASSWORD}
        if chart_id is not None:
            body['guest_chart_id'] = chart_id
        return self.client.post('/auth/login', json=body,
                                headers=headers if headers is not None else {**self.tracked('authorization'), **self.guest})

    def guest_chart(self, token=None):
        with self.quiet():
            return self.chart(user_id=None, session_token=token or self.guest_token, related=False)

    def seed_charts(self, count, user_id=1):
        with self.quiet():
            for _ in range(count):
                self.chart(user_id=user_id, related=False)

    def set_plan(self, plan):
        with self.quiet(), self.sessions() as db:
            user = db.get(User, 1)
            user.plan = plan
            now = datetime.utcnow()
            user.current_period_start, user.current_period_end = (now - timedelta(days=1), now + timedelta(days=29)) if plan == 'premium' else (None, None)
            db.commit()

    def create_chart(self):
        with self.mock_calculations():
            return self.client.post('/natal-chart', headers={**self.tracked('chart_creation'), **self.owner}, json=fixtures.PAYLOAD)


# --------------------------------------------------------------------------- B2 (collector)
class Metrics2CollectorTests(Metrics2Base):
    def send(self, events):
        return self.client.post('/analytics/events', headers={**self.origin, 'X-Lunaria-Analytics-Revision': self.revision},
                                json={'policy_version': 'posthog-v1', 'visitor_id': self.visitor, 'events': events})

    def record(self, name, properties, route='chart_view'):
        return dict(event_id=str(uuid4()), session_id=self.analytics_session, name=name, route_key=route, properties=properties)

    def test_b2_collector_accepts_new_client_events_and_refuses_server_names(self):
        self.choose()
        batch = [self.record('wheel_tooltip_opened', {'object': 'point'}),
                 self.record('wheel_question_selected', {'object': 'aspect'}),
                 self.record('site_error_occurred', {'kind': 'request'}, route='my_charts')]
        response = self.send(batch)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {'accepted': 3})
        with self.sessions() as db:
            rows = {row.event_name: dict(row.properties) for row in db.query(AnalyticsOutbox).all()}
        for item in batch:
            properties = rows[item['name']]
            self.assertEqual({k: properties[k] for k in item['properties']}, item['properties'])
            self.assertLessEqual(set(properties), set(item['properties']) | STANDARD)
        refused = [self.record('guest_chart_saved', {'method': 'email'}),
                   self.record('payment_failed', {'provider': 'stripe', 'stage': 'renewal'}),
                   self.record('free_limit_would_block', {'resource': 'chart'}),
                   self.record('site_error_occurred', {'kind': 'script', 'message': 'CANARY TypeError at app.js'}),
                   self.record('wheel_tooltip_opened', {'object': 'point', 'planet': 'CANARY Venus'})]
        for item in refused:
            with self.subTest(name=item['name']):
                response = self.send([item])
                self.assertEqual(response.status_code, 422)
                self.assertNotIn('CANARY', response.text)


# --------------------------------------------------------------------------- B4, B10 (transfer), B11 (transfer)
class Metrics2GuestChartTests(Metrics2Base):
    def guest_headers(self, page='authorization'):
        return {**self.tracked(page), **self.guest}

    def assert_saved(self, method, user_id, count=1):
        rows = self.events('guest_chart_saved')
        self.assertEqual(len(rows), count, rows)
        for row in rows:
            self.assertEqual(row['properties']['method'], method)
            self.assertEqual(row['user_id'], user_id)
            self.assertTrue(row['distinct_id'].startswith('account_'))

    def test_b4_verify_code_transfer_is_one_email_event(self):
        chart_id = self.guest_chart()
        self.choose()
        self.verification_code('metrics-new@example.com')
        response = self.client.post('/auth/verify-code?code=123456', headers=self.guest_headers(), json={
            'email': 'metrics-new@example.com', 'password': PASSWORD, 'guest_chart_id': chart_id})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['guest_chart_migration']['status'], 'migrated')
        with self.sessions() as db:
            user_id = db.query(User).filter_by(email='metrics-new@example.com').one().id
        self.assert_saved('email', user_id)

    def test_b4_login_transfer_once_and_repeat_login_adds_nothing(self):
        self.set_password()
        chart_id = self.guest_chart()
        self.choose()
        first = self.login(chart_id)
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.json()['guest_chart_migration']['status'], 'migrated')
        again = self.login(chart_id)
        self.assertEqual(again.json()['guest_chart_migration']['status'], 'not_found')
        self.assert_saved('email', 1)

    def test_b4_google_transfer_is_one_google_event(self):
        chart_id = self.guest_chart()
        self.choose()
        with patch.dict(os.environ, {'GOOGLE_CLIENT_ID': google_fx.CLIENT}), \
                patch('google.oauth2.id_token._fetch_certs', return_value={'test-key': google_fx.public}):
            body = {'credential': google_fx.credential(), 'nonce': google_fx.NONCE, 'guest_chart_id': chart_id}
            first = self.client.post('/auth/google', json=body, headers=self.guest_headers())
            self.assertEqual(first.status_code, 200, first.text)
            self.assertEqual(first.json()['guest_chart_migration']['status'], 'migrated')
            again = self.client.post('/auth/google', json={**body, 'credential': google_fx.credential()}, headers=self.guest_headers())
            self.assertEqual(again.json()['guest_chart_migration']['status'], 'not_found')
        with self.sessions() as db:
            user_id = db.query(User).filter_by(google_sub='google-123').one().id
        self.assert_saved('google', user_id)

    def test_b4_not_found_not_requested_and_limit_reached_write_no_event(self):
        self.set_password()
        self.choose()
        foreign = self.guest_chart(token=uuid4())
        self.assertEqual(self.login(foreign).json()['guest_chart_migration']['status'], 'not_found')
        self.assertEqual(self.login(None).json()['guest_chart_migration']['status'], 'not_requested')
        self.seed_charts(3)
        own = self.guest_chart()
        self.assertEqual(self.login(own).json()['guest_chart_migration']['status'], 'limit_reached')
        self.assertEqual(self.events('guest_chart_saved'), [])
        # Positive control: the same channel records a real transfer.
        self.set_plan('premium')
        self.assertEqual(self.login(own).json()['guest_chart_migration']['status'], 'migrated')
        self.assert_saved('email', 1)

    def test_b4_no_consent_and_account_chart_creation_give_no_event(self):
        self.set_password()
        first = self.guest_chart()
        response = self.login(first)  # analytics headers, but this visitor never consented
        self.assertEqual(response.json()['guest_chart_migration']['status'], 'migrated')
        self.assertEqual(self.events('guest_chart_saved'), [])
        with self.sessions() as db:
            self.assertEqual(db.query(AnalyticsOutbox).count(), 0)
        self.choose(owner=self.owner)
        self.assertEqual(self.create_chart().status_code, 200)
        self.assertEqual(self.events('guest_chart_saved'), [])
        self.assertEqual(len(self.events('chart_creation_completed')), 1)
        second = self.guest_chart()  # positive control after consent
        self.assertEqual(self.login(second).json()['guest_chart_migration']['status'], 'migrated')
        self.assert_saved('email', 1)

    def test_b10_analytics_failure_does_not_change_transfer(self):
        self.set_password()
        self.choose()
        # api.auth calls capture_business directly for login_succeeded (pre-existing, out of scope);
        # the transfer fact travels observe_flush -> observe_commit -> capture_business.
        modes = [('capture_raises', lambda: everywhere('capture_business', exclude=('api.auth',), side_effect=RuntimeError('CANARY'))),
                 ('queue_full', lambda: patch.object(analytics, '_facts', full_queue())),
                 ('persist_fails', lambda: patch.object(analytics, 'enqueue', side_effect=OperationalError('CANARY', {}, Exception())))]
        for label, mode in modes:
            with self.subTest(mode=label):
                chart_id = self.guest_chart()
                with mode():
                    response = self.login(chart_id)
                    if label == 'persist_fails':
                        self.drain()
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()['guest_chart_migration'], {'status': 'migrated', 'chart_id': chart_id})
                with self.sessions() as db:
                    chart = db.get(NatalChart, chart_id)
                    self.assertEqual((chart.user_id, chart.session_token), (1, None))
                self.clear_facts()

    def test_b11_guest_transfer_at_chart_limit_is_one_shadow_event(self):
        self.set_password()
        self.choose()
        self.seed_charts(3)
        chart_id = self.guest_chart()
        with self.beta():
            response = self.login(chart_id)
            self.assertEqual(response.json()['guest_chart_migration']['status'], 'migrated')
            rows = self.events('free_limit_would_block')
        self.assertEqual([row['properties']['resource'] for row in rows], ['chart'])
        self.assertEqual(rows[0]['user_id'], 1)
        self.assertTrue(rows[0]['distinct_id'].startswith('account_'))


# --------------------------------------------------------------------------- B5, B10 (checkout)
class Metrics2CheckoutFailureTests(Metrics2Base):
    def checkout(self, provider, headers=None):
        return self.client.post(f'/payments/{provider}/checkout', headers=headers or {**self.tracked('pricing'), **self.owner})

    def failures(self):
        return self.events('payment_failed')

    def test_b5_server_side_checkout_failures_are_one_event_each(self):
        self.choose(owner=self.owner)
        # Real Stripe path: provider read fails -> 502 CHECKOUT_UNKNOWN (stripe_billing.create_checkout).
        with patch.dict(os.environ, stripe_fx.ENV), patch.object(stripe_billing, 'api', side_effect=RuntimeError('CANARY provider')):
            response = self.checkout('stripe')
        self.assertEqual((response.status_code, response.json()['detail']), (502, stripe_billing.CHECKOUT_UNKNOWN))
        expected = [{'provider': 'stripe', 'stage': 'checkout'}]
        self.assertEqual([self.core(row) for row in self.failures()], expected)
        cases = [('stripe', HTTPException(503, stripe_billing.STRIPE_UNAVAILABLE)),
                 ('lava', lava_client.InvoiceNotCreated(502, lava_client.PROVIDER_SILENT)),
                 ('lava', lava_client.InvoiceNotCreated(503, lava_client.PROVIDER_SILENT, headers={'Retry-After': '60'})),
                 ('lava', HTTPException(502, 'Оплату нужно проверить. Обратитесь в поддержку перед повторной оплатой.'))]
        for provider, error in cases:
            with self.subTest(provider=provider, status=error.status_code, kind=type(error).__name__):
                module = stripe_billing if provider == 'stripe' else lava_billing
                with patch.object(module, 'create_checkout', side_effect=error):
                    response = self.checkout(provider)
                self.assertEqual(response.status_code, error.status_code)
                self.assertEqual(response.json()['detail'], error.detail)
                if error.headers:
                    self.assertEqual(response.headers.get('retry-after'), '60')
                expected.append({'provider': provider, 'stage': 'checkout'})
                rows = self.failures()
                self.assertEqual([self.core(row) for row in rows], expected)
        self.assertTrue(all(row['user_id'] == 1 and row['distinct_id'].startswith('account_') for row in rows))

    def test_b5_refusals_and_missing_consent_count_nothing(self):
        self.choose(owner=self.owner)
        for provider in ('stripe', 'lava'):
            module = stripe_billing if provider == 'stripe' else lava_billing
            for status in (409, 422):
                with self.subTest(provider=provider, status=status):
                    with patch.object(module, 'create_checkout', side_effect=HTTPException(status, 'Отказ.')):
                        response = self.checkout(provider)
                    self.assertEqual((response.status_code, response.json()['detail']), (status, 'Отказ.'))
        stranger = self.tracked('pricing', visitor=str(uuid4()), revision=str(uuid4()))  # no consent row
        with patch.object(stripe_billing, 'create_checkout', side_effect=HTTPException(502, stripe_billing.CHECKOUT_UNKNOWN)):
            self.assertEqual(self.checkout('stripe', {**stranger, **self.owner}).status_code, 502)
        self.assertEqual(self.failures(), [])
        with patch.object(lava_billing, 'create_checkout', side_effect=lava_client.InvoiceNotCreated(502, lava_client.PROVIDER_SILENT)):
            self.assertEqual(self.checkout('lava').status_code, 502)  # positive control
        self.assertEqual([self.core(row) for row in self.failures()], [{'provider': 'lava', 'stage': 'checkout'}])

    def test_b10_analytics_failure_does_not_change_checkout_answer(self):
        self.choose(owner=self.owner)
        modes = [('capture_raises', lambda: everywhere('capture_business', side_effect=RuntimeError('CANARY'))),
                 ('queue_full', lambda: patch.object(analytics, '_facts', full_queue())),
                 ('persist_fails', lambda: patch.object(analytics, 'enqueue', side_effect=OperationalError('CANARY', {}, Exception())))]
        for label, mode in modes:
            for provider, error in (('stripe', HTTPException(502, stripe_billing.CHECKOUT_UNKNOWN)),
                                    ('lava', lava_client.InvoiceNotCreated(503, lava_client.PROVIDER_SILENT, headers={'Retry-After': '60'}))):
                with self.subTest(mode=label, provider=provider):
                    module = stripe_billing if provider == 'stripe' else lava_billing
                    with mode(), patch.object(module, 'create_checkout', side_effect=error):
                        response = self.checkout(provider)
                        if label == 'persist_fails':
                            self.drain()
                    self.assertEqual((response.status_code, response.json()['detail']), (error.status_code, error.detail))
                    self.assertNotIn('CANARY', response.text)
                    self.clear_facts()


# --------------------------------------------------------------------------- B11, B12, B13 (shadow paywall)
class ShadowHelpers(Metrics2Base):
    def seed(self):
        self.chart_id = self.chart(related=False)  # user 1 owns one chart

    def seed_answers(self, count):
        with self.quiet(), self.sessions() as db:
            db.add_all([GPTUsage(user_id=1, chart_id=self.chart_id, status='succeeded', plan='free') for _ in range(count)])
            db.commit()

    def ask(self, question='Что важно для меня в работе?', fail=False):
        with patch('api.endpoints.ChartInterpreter') as provider:
            provider.return_value.ask_gpt = AsyncMock(side_effect=RuntimeError('synthetic provider failure') if fail else None,
                                                      return_value='Synthetic answer')
            return self.client.post('/ask-gpt', headers={**self.tracked('chart_view'), **self.owner},
                                    json={'chart_id': self.chart_id, 'question': question})

    def shadow(self, resource):
        return [row for row in self.events('free_limit_would_block') if row['properties'].get('resource') == resource]

    def usage(self):
        response = self.client.get('/account/usage', headers=self.owner)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()


class Metrics2ShadowLimitTests(ShadowHelpers):
    # B11 -----------------------------------------------------------------
    def test_b11_fourth_chart_in_beta_is_one_shadow_event_and_fifth_adds_none(self):
        self.choose(owner=self.owner)
        self.seed_charts(2)  # 3 charts = the free limit
        with self.beta():
            self.assertEqual(self.create_chart().status_code, 200)
            rows = self.shadow('chart')
            self.assertEqual(len(rows), 1, rows)
            self.assertEqual((rows[0]['user_id'], rows[0]['properties']['resource']), (1, 'chart'))
            self.assertTrue(rows[0]['distinct_id'].startswith('account_'))
            self.assertEqual(self.create_chart().status_code, 200)
            self.assertEqual(len(self.shadow('chart')), 1)

    def test_b11_below_limit_premium_and_beta_off_give_no_event(self):
        self.choose(owner=self.owner)
        self.seed_charts(1)  # 2 charts
        with self.beta():
            self.assertEqual(self.create_chart().status_code, 200)  # 3rd chart: at the limit, not over it
            self.assertEqual(self.shadow('chart'), [])
            self.set_plan('premium')
            self.assertEqual(self.create_chart().status_code, 200)  # 4th, Premium
            self.assertEqual(self.shadow('chart'), [])
        self.set_plan('free')
        with self.beta(False):
            refused = self.create_chart()
            self.assertEqual(refused.status_code, 409)
            self.assertEqual(refused.json()['detail']['code'], 'CHART_LIMIT_REACHED')
            self.assertEqual(self.shadow('chart'), [])
        with self.beta():
            self.assertEqual(self.create_chart().status_code, 200)  # positive control
            self.assertEqual(len(self.shadow('chart')), 1)

    # B12 -----------------------------------------------------------------
    def test_b12_eleventh_answer_in_beta_is_one_shadow_event_and_twelfth_adds_none(self):
        self.choose(owner=self.owner)
        self.seed_answers(10)
        with self.beta():
            response = self.ask()
            self.assertEqual(response.status_code, 200, response.text)
            rows = self.shadow('question')
            self.assertEqual(len(rows), 1, rows)
            self.assertEqual(rows[0]['user_id'], 1)
            self.assertTrue(rows[0]['distinct_id'].startswith('account_'))
            self.assertEqual(self.ask().status_code, 200)
            self.assertEqual(len(self.shadow('question')), 1)
            self.assertEqual(len(self.events('ai_answer_completed')), 2)

    def test_b12_below_limit_failed_answer_premium_and_beta_off_give_no_event(self):
        self.choose(owner=self.owner)
        self.seed_answers(9)
        with self.beta():
            self.assertEqual(self.ask().status_code, 200)  # 10th success: at the limit
            self.assertEqual(self.shadow('question'), [])
            self.assertEqual(self.ask(fail=True).status_code, 500)  # released, not a success
            self.assertEqual(self.shadow('question'), [])
            with self.sessions() as db:
                self.assertEqual(db.query(GPTUsage).filter_by(status='released').count(), 1)
            self.set_plan('premium')
            self.assertEqual(self.ask().status_code, 200)
            self.assertEqual(self.shadow('question'), [])
        self.set_plan('free')
        with self.beta(False):
            refused = self.ask()
            self.assertEqual(refused.status_code, 409)
            self.assertEqual(refused.json()['detail']['code'], 'GPT_LIMIT_REACHED')
            self.assertEqual(self.shadow('question'), [])
        with self.beta():
            self.assertEqual(self.ask().status_code, 200)  # positive control
            self.assertEqual(len(self.shadow('question')), 1)

    # B13 (guard) ---------------------------------------------------------
    def test_b13_beta_answers_limits_and_usage_are_unchanged_over_the_limit(self):
        self.choose(owner=self.owner)
        self.seed_charts(3)  # 4 charts
        self.seed_answers(11)
        with self.beta():
            before = self.usage()
            self.assertEqual((before['plan'], before['free_beta_enabled'], before['gpt_limit_type']), ('free', True, 'unlimited'))
            self.assertEqual((before['saved_charts_used'], before['saved_charts_limit']), (4, None))
            self.assertEqual((before['gpt_messages_used'], before['gpt_messages_limit'], before['gpt_messages_available']), (11, None, None))
            created = self.create_chart()
            self.assertEqual(created.status_code, 200, created.text)
            answer = self.ask()
            self.assertEqual(answer.status_code, 200, answer.text)
            self.assertEqual(answer.json()['response'], 'Synthetic answer')
            self.drain()
            after = self.usage()
        self.assertEqual({k: v for k, v in after.items() if k not in ('saved_charts_used', 'gpt_messages_used')},
                         {k: v for k, v in before.items() if k not in ('saved_charts_used', 'gpt_messages_used')})
        self.assertEqual((after['saved_charts_used'], after['gpt_messages_used']), (5, 12))
        with self.sessions() as db:
            self.assertEqual(db.get(User, 1).plan, 'free')

    def test_b13_shadow_count_failure_keeps_the_main_event(self):
        self.choose(owner=self.owner)
        self.seed_charts(3)
        self.seed_answers(11)
        pattern = re.compile(r'\bfrom\s+(natal_charts|gpt_usage)\b', re.I)

        def fail_counts(conn, cursor, statement, parameters, context, executemany):
            if pattern.search(statement):
                raise OperationalError('synthetic count failure', {}, Exception())

        with self.beta():
            self.assertEqual(self.create_chart().status_code, 200)
            self.assertEqual(self.ask().status_code, 200)
            sa_event.listen(self.engine, 'before_cursor_execute', fail_counts)
            try:
                self.drain()  # the worker step alone sees the failing count queries
            finally:
                sa_event.remove(self.engine, 'before_cursor_execute', fail_counts)
        self.assertEqual(len(self.events('chart_creation_completed', drain=False)), 1)
        self.assertEqual(len(self.events('ai_answer_completed', drain=False)), 1)


# --------------------------------------------------------------------------- B6, B8, B9, B10, B14 (Stripe)
class Metrics2StripeWebhookTests(Metrics2Base):
    send = stripe_fx.StripeTests.send

    def seed(self):
        env = patch.dict(os.environ, stripe_fx.ENV); env.start(); self.addCleanup(env.stop)
        self.start = datetime.utcnow().replace(microsecond=0) - timedelta(days=2)
        self.end = self.start + timedelta(days=31)
        self.sub = stripe_fx.subscription(self.start, self.end)
        self.invoices = {stripe_fx.INVOICE: stripe_fx.invoice(self.start, self.end)}
        self.session = {'id': stripe_fx.SESSION, 'livemode': True, 'status': 'open', 'mode': 'subscription',
            'customer': stripe_fx.CUSTOMER, 'subscription': stripe_fx.SUB, 'metadata': {'checkout_binding': 'bound', 'user_id': '1'},
            'client_secret': stripe_fx.SESSION + '_secret_test', 'expires_at': int(datetime.utcnow().timestamp()) + 3600}
        with self.sessions() as db:
            db.add(BillingCustomer(id='customer-binding', user_id=1, provider='stripe', account_id=stripe_fx.ACCOUNT,
                                   livemode=True, customer_id=stripe_fx.CUSTOMER))
            db.add(StripeCheckout(id='bound', user_id=1, account_id=stripe_fx.ACCOUNT, livemode=True, customer_id=stripe_fx.CUSTOMER,
                                  session_id=stripe_fx.SESSION, subscription_id=stripe_fx.SUB, state='ready'))
            db.commit()
        stripe_fx.install_fake(self)

    def failed_invoice(self, invoice_id, reason='subscription_cycle', offset=1):
        start = self.end + timedelta(days=30 * (offset - 1))
        value = stripe_fx.invoice(start, start + timedelta(days=30), invoice_id)
        value['status'], value['billing_reason'] = 'open', reason
        self.invoices[invoice_id] = value
        return stripe_fx.event('invoice.payment_failed', invoice_id)

    def grant(self):
        response = self.send()
        self.assertEqual(response.json()['outcome'], 'granted', response.text)
        self.sub['status'] = 'past_due'

    def ledger(self):
        with self.sessions() as db:
            user = db.get(User, 1)
            return dict(events=sorted((e.event_id, e.outcome) for e in db.query(BillingEvent).all()),
                        periods=sorted((p.id, p.invoice_id, p.period_start, p.applied) for p in db.query(BillingPeriod).all()),
                        checkout=(db.get(StripeCheckout, 'bound').state, db.get(StripeCheckout, 'bound').revision),
                        user=(user.plan, user.payment_provider, user.subscription_status, user.current_period_end))

    def mismatch(self, invoice_id=stripe_fx.INVOICE):
        self.mismatches = getattr(self, 'mismatches', 0) + 1  # distinct starts: uq_billing_period_start
        with self.quiet(), self.sessions() as db:
            db.add(BillingPeriod(id='mismatch-' + invoice_id, user_id=1, provider='stripe', account_id=stripe_fx.ACCOUNT, livemode=True,
                                 subscription_id=stripe_fx.SUB, invoice_id=invoice_id, period_start=self.start + timedelta(days=1, minutes=self.mismatches),
                                 period_end=self.end, applied=False))
            db.commit()

    # B6 ------------------------------------------------------------------
    def test_b6_renewal_failure_is_one_event_per_invoice(self):
        self.choose(owner=self.owner)
        self.grant()
        value = self.failed_invoice('in_failed')
        first = self.send(value)
        self.assertEqual((first.status_code, first.json()['outcome']), (200, 'recorded'), first.text)
        self.assertEqual(self.send(value).json()['outcome'], 'duplicate')
        retry = stripe_fx.event('invoice.payment_failed', 'in_failed')  # Stripe's next attempt on the same invoice
        self.assertEqual(self.send(retry).json()['outcome'], 'recorded')
        rows = self.events('payment_failed')
        self.assertEqual([self.core(row) for row in rows], [{'provider': 'stripe', 'stage': 'renewal'}])
        self.assertEqual(rows[0]['user_id'], 1)
        self.assertTrue(rows[0]['distinct_id'].startswith('account_'))

    def test_b6_first_payment_failure_is_one_first_payment_event(self):
        self.choose(owner=self.owner)
        self.invoices[stripe_fx.INVOICE].update(status='open', billing_reason='subscription_create')
        self.sub['status'] = 'incomplete'
        response = self.send(stripe_fx.event('invoice.payment_failed'))
        self.assertEqual((response.status_code, response.json()['outcome']), (200, 'recorded'), response.text)
        self.assertEqual([self.core(row) for row in self.events('payment_failed')],
                         [{'provider': 'stripe', 'stage': 'first_payment'}])
        with self.sessions() as db:
            self.assertEqual(db.get(User, 1).plan, 'free')

    def test_b6_invoice_reread_as_paid_is_not_a_failure(self):
        self.choose(owner=self.owner)
        response = self.send(stripe_fx.event('invoice.payment_failed'))  # the invoice was paid meanwhile
        self.assertEqual(response.json()['outcome'], 'granted', response.text)
        self.assertEqual(self.events('payment_failed'), [])
        self.sub['status'] = 'past_due'
        self.assertEqual(self.send(self.failed_invoice('in_failed')).json()['outcome'], 'recorded')  # positive control
        self.assertEqual([self.core(row) for row in self.events('payment_failed')], [{'provider': 'stripe', 'stage': 'renewal'}])

    # B8 ------------------------------------------------------------------
    def test_b8_webhook_needs_prior_account_consent(self):
        self.grant()
        # 1. no consent row at all
        self.assertEqual(self.send(self.failed_invoice('in_f1', offset=1)).json()['outcome'], 'recorded')
        self.assertEqual(self.events('payment_failed'), [])
        # 2. consent given after the event: the fact is stamped 5 s before the consent (controlled clock)
        real_now = analytics.now
        with patch.object(analytics, 'now', lambda: real_now() - timedelta(seconds=5)):
            self.assertEqual(self.send(self.failed_invoice('in_f2', offset=2)).json()['outcome'], 'recorded')
        self.choose(owner=self.owner)
        self.assertEqual(self.events('payment_failed'), [])
        # 3. consent row present, account setting switched off
        with self.sessions() as db:
            db.get(AnalyticsAccountPreference, 1).accepted = False; db.commit()
        self.assertEqual(self.send(self.failed_invoice('in_f3', offset=3)).json()['outcome'], 'recorded')
        self.assertEqual(self.events('payment_failed'), [])
        # 4. positive control: setting back on, a new failed invoice is sent on behalf of the account
        with self.sessions() as db:
            db.get(AnalyticsAccountPreference, 1).accepted = True; db.commit()
        self.assertEqual(self.send(self.failed_invoice('in_f4', offset=4)).json()['outcome'], 'recorded')
        rows = self.events('payment_failed')
        self.assertEqual([self.core(row) for row in rows], [{'provider': 'stripe', 'stage': 'renewal'}])
        self.assertTrue(rows[0]['distinct_id'].startswith('account_'))
        self.assertEqual(rows[0]['user_id'], 1)

    # B9 ------------------------------------------------------------------
    def test_b9_paid_period_mismatch_after_ownership_is_one_confirmation_event(self):
        self.choose(owner=self.owner)
        self.mismatch()
        before = self.ledger()
        value = stripe_fx.event()
        for _ in range(3):  # provider redelivers the same event
            response = self.send(value)
            self.assertEqual((response.status_code, response.json()['detail']), (400, 'Invalid or unverified Stripe event'))
        self.assertEqual(self.ledger(), before)
        rows = self.events('payment_failed')
        self.assertEqual([self.core(row) for row in rows], [{'provider': 'stripe', 'stage': 'confirmation'}])
        self.assertEqual(rows[0]['user_id'], 1)

    def test_b9_refusals_before_ownership_give_no_event(self):
        self.choose(owner=self.owner)
        self.assertEqual(self.send(stripe_fx.event(), signature='t=1,v1=bad').status_code, 401)
        wrong_version = stripe_fx.event(); wrong_version['api_version'] = '2020-01-01'
        self.assertEqual(self.send(wrong_version).status_code, 400)
        self.sub['metadata']['user_id'] = '2'  # subscription belongs to another account
        self.assertEqual(self.send(stripe_fx.event()).status_code, 400)
        self.sub['metadata'].update(user_id='1', checkout_binding='unknown-binding')
        self.invoices['in_unbound'] = stripe_fx.invoice(self.start, self.end, 'in_unbound', subscription='sub_unbound')
        self.assertEqual(self.send(stripe_fx.event(object_id='in_unbound')).status_code, 503)  # no binding
        self.assertEqual(self.events('payment_failed'), [])
        self.sub['metadata']['checkout_binding'] = 'bound'
        self.mismatch()  # positive control
        self.assertEqual(self.send(stripe_fx.event()).status_code, 400)
        self.assertEqual([self.core(row) for row in self.events('payment_failed')], [{'provider': 'stripe', 'stage': 'confirmation'}])

    # B10 (guard) ---------------------------------------------------------
    def test_b10_analytics_failure_does_not_change_webhook_result(self):
        self.choose(owner=self.owner)
        self.grant()
        modes = [('capture_raises', lambda: everywhere('capture_business', side_effect=RuntimeError('CANARY'))),
                 ('queue_full', lambda: patch.object(analytics, '_facts', full_queue())),
                 ('persist_fails', lambda: patch.object(analytics, 'enqueue', side_effect=OperationalError('CANARY', {}, Exception())))]
        for offset, (label, mode) in enumerate(modes, start=1):
            with self.subTest(mode=label):
                value = self.failed_invoice(f'in_guard{offset}', offset=offset)
                with mode():
                    response = self.send(value)
                    if label == 'persist_fails':
                        self.drain()
                self.assertEqual((response.status_code, response.json()), (200, {'received': True, 'outcome': 'recorded'}))
                key = 'stripe:' + stripe_fx.ACCOUNT + ':live:' + value['id']
                with self.sessions() as db:
                    self.assertEqual(db.get(BillingEvent, key).outcome, 'recorded')
                    self.assertEqual(db.get(User, 1).plan, 'premium')
                    self.assertEqual(db.query(BillingPeriod).filter_by(applied=True).count(), 1)
                # confirmation path: still 400 and nothing written
                invoice_id = f'in_mm{offset}'
                self.invoices[invoice_id] = stripe_fx.invoice(self.end, self.end + timedelta(days=30), invoice_id)
                self.mismatch(invoice_id)
                before = self.ledger()
                with mode():
                    refused = self.send(stripe_fx.event(object_id=invoice_id))
                    if label == 'persist_fails':
                        self.drain()
                self.assertEqual((refused.status_code, refused.json()['detail']), (400, 'Invalid or unverified Stripe event'))
                self.assertEqual(self.ledger(), before)
                self.clear_facts()

    # B14 -----------------------------------------------------------------
    def test_b14_webhook_payment_failed_carries_only_closed_keys(self):
        with self.quiet(), self.sessions() as db:
            db.get(User, 1).email = 'canary-owner@example.com'; db.commit()
        self.choose(owner=self.owner)
        self.grant()
        self.send(self.failed_invoice('in_CANARYinvoice'))
        self.mismatch('in_CANARYmm')
        self.invoices['in_CANARYmm'] = stripe_fx.invoice(self.end, self.end + timedelta(days=30), 'in_CANARYmm')
        self.send(stripe_fx.event(object_id='in_CANARYmm'))
        rows = self.events('payment_failed')
        self.assertEqual(sorted(row['properties']['stage'] for row in rows), ['confirmation', 'renewal'])
        self.assert_closed(rows, 'payment_failed', canaries=('canary', '999', stripe_fx.SUB, stripe_fx.CUSTOMER, stripe_fx.ACCOUNT, 'evt_'))


# --------------------------------------------------------------------------- B7, B8, B9, B10, B14 (Lava)
class Metrics2LavaWebhookTests(Metrics2Base):
    provider = lava_fx.LavaTests.provider
    event = lava_fx.LavaTests.event
    send = lava_fx.LavaTests.send

    def seed(self):
        env = patch.dict(os.environ, lava_fx.ENV); env.start(); self.addCleanup(env.stop)
        self.start = datetime.utcnow() - timedelta(days=2)
        self.end = self.start + timedelta(days=31)
        self.snapshot = {'id': lava_fx.CONTRACT, 'datetime': lava_fx.iso(self.start), 'status': 'COMPLETED',
            'receipt': {'amount': 799, 'currency': 'RUB'}, 'buyer': {'email': 'a@example.test'},
            'periodicity': 'MONTHLY', 'subscriptionStatus': 'ACTIVE', 'expiredAt': lava_fx.iso(self.end),
            'cancelledAt': None, 'terminatedAt': None, 'recurrentPayments': []}
        self.catalog = lava_fx.catalog()
        self.invoice = {'id': str(uuid4()), 'status': 'NEW', 'amountTotal': {'amount': 799, 'currency': 'RUB'},
                        'paymentUrl': 'https://pay.example.test/invoice'}
        network = patch('modules.lava_client.request', side_effect=self.provider)
        self.network = network.start(); self.addCleanup(network.stop)
        with self.sessions() as db:
            db.add(LavaCheckout(id='bound', user_id=1, contract_id=lava_fx.CONTRACT, offer_id=lava_fx.OFFER,
                product_id=lava_fx.PRODUCT, buyer_email='a@example.test', state='ready', created_at=self.start - timedelta(seconds=10)))
            db.commit()

    def failed_snapshot(self):
        self.snapshot.update(status='FAILED', subscriptionStatus='FAILED', expiredAt=None)

    def ledger(self):
        with self.sessions() as db:
            user = db.get(User, 1)
            return dict(events=sorted((e.event_id, e.outcome) for e in db.query(BillingEvent).all()),
                        checkout=db.get(LavaCheckout, 'bound').state,
                        user=(user.plan, user.payment_provider, user.provider_subscription_id, user.subscription_status, user.current_period_end))

    # B7 ------------------------------------------------------------------
    def test_b7_first_payment_failure_is_one_event(self):
        self.choose(owner=self.owner)
        self.failed_snapshot()
        value = self.event('payment.failed')
        response = self.send(value)
        self.assertEqual((response.status_code, response.json()['outcome']), (200, 'processed'), response.text)
        self.assertEqual(self.send(value).json()['outcome'], 'duplicate')
        rows = self.events('payment_failed')
        self.assertEqual([self.core(row) for row in rows], [{'provider': 'lava', 'stage': 'first_payment'}])
        self.assertEqual(rows[0]['user_id'], 1)
        self.assertTrue(rows[0]['distinct_id'].startswith('account_'))

    def test_b7_recurring_failure_is_one_renewal_event(self):
        self.choose(owner=self.owner)
        self.assertEqual(self.send(self.event()).status_code, 200)
        self.snapshot['subscriptionStatus'] = 'FAILED'
        value = self.event('subscription.recurring.payment.failed', datetime.utcnow())
        response = self.send(value)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.send(value).json()['outcome'], 'duplicate')
        self.assertEqual([self.core(row) for row in self.events('payment_failed')], [{'provider': 'lava', 'stage': 'renewal'}])
        with self.sessions() as db:
            self.assertEqual((db.get(User, 1).plan, db.get(User, 1).subscription_status), ('premium', 'past_due'))

    def test_b7_failure_not_confirmed_by_api_gives_no_event(self):
        self.choose(owner=self.owner)
        self.snapshot.update(status='IN_PROGRESS', subscriptionStatus='ACTIVE')
        value = self.event('payment.failed')
        self.assertEqual(self.send(value).status_code, 503)
        self.assertEqual(self.events('payment_failed'), [])
        self.failed_snapshot()  # positive control: the API now confirms the same delivery
        self.assertEqual(self.send(value).status_code, 200)
        self.assertEqual([self.core(row) for row in self.events('payment_failed')], [{'provider': 'lava', 'stage': 'first_payment'}])

    # B8 ------------------------------------------------------------------
    def test_b8_lava_webhook_without_account_consent_gives_no_event(self):
        self.failed_snapshot()
        self.assertEqual(self.send(self.event('payment.failed')).status_code, 200)
        self.assertEqual(self.events('payment_failed'), [])
        self.choose(owner=self.owner)  # positive control: a later failure after consent
        self.assertEqual(self.send(self.event('payment.failed', datetime.utcnow())).status_code, 200)
        rows = self.events('payment_failed')
        self.assertEqual(len(rows), 1, rows)
        self.assertTrue(rows[0]['distinct_id'].startswith('account_'))

    # B9 ------------------------------------------------------------------
    def test_b9_missing_paid_period_after_binding_is_one_confirmation_event(self):
        self.choose(owner=self.owner)
        self.snapshot['expiredAt'] = None
        before = self.ledger()
        value = self.event()
        for _ in range(3):
            response = self.send(value)
            self.assertEqual((response.status_code, response.json()['detail']), (400, 'Invalid or unverified Lava event'))
        self.assertEqual(self.ledger(), before)
        rows = self.events('payment_failed')
        self.assertEqual([self.core(row) for row in rows], [{'provider': 'lava', 'stage': 'confirmation'}])
        self.assertEqual(rows[0]['user_id'], 1)

    def test_b9_lava_refusals_before_binding_give_no_event(self):
        self.choose(owner=self.owner)
        self.assertEqual(self.send(self.event(), secret='wrong').status_code, 401)
        self.assertEqual(self.send({'eventType': 'payment.success'}).status_code, 400)
        unbound = self.event(); unbound['contractId'] = str(uuid4())
        self.assertEqual(self.send(unbound).status_code, 503)
        foreign = self.event(); foreign['buyer']['email'] = 'b@example.test'
        self.assertEqual(self.send(foreign).status_code, 400)
        self.assertEqual(self.events('payment_failed'), [])
        self.snapshot['expiredAt'] = None  # positive control
        self.assertEqual(self.send(self.event()).status_code, 400)
        self.assertEqual([self.core(row) for row in self.events('payment_failed')], [{'provider': 'lava', 'stage': 'confirmation'}])

    # B10 (guard) ---------------------------------------------------------
    def test_b10_analytics_failure_does_not_change_lava_result(self):
        self.choose(owner=self.owner)
        self.failed_snapshot()
        modes = [('capture_raises', lambda: everywhere('capture_business', side_effect=RuntimeError('CANARY'))),
                 ('queue_full', lambda: patch.object(analytics, '_facts', full_queue())),
                 ('persist_fails', lambda: patch.object(analytics, 'enqueue', side_effect=OperationalError('CANARY', {}, Exception())))]
        for minutes, (label, mode) in enumerate(modes, start=1):
            with self.subTest(mode=label):
                value = self.event('payment.failed', self.start + timedelta(minutes=minutes))
                with mode():
                    response = self.send(value)
                    if label == 'persist_fails':
                        self.drain()
                self.assertEqual((response.status_code, response.json()), (200, {'received': True, 'outcome': 'processed'}))
                with self.sessions() as db:
                    self.assertEqual(db.get(LavaCheckout, 'bound').state, 'failed')
                    self.assertIsNone(db.get(User, 1).payment_provider)
                self.clear_facts()
        self.snapshot.update(status='COMPLETED', subscriptionStatus='ACTIVE', expiredAt=None)
        before = self.ledger()
        with everywhere('capture_business', side_effect=RuntimeError('CANARY')):
            refused = self.send(self.event('payment.success', datetime.utcnow()))
        self.assertEqual((refused.status_code, refused.json()['detail']), (400, 'Invalid or unverified Lava event'))
        self.assertEqual(self.ledger(), before)

    # B14 -----------------------------------------------------------------
    def test_b14_lava_payment_failed_carries_only_closed_keys(self):
        self.choose(owner=self.owner)
        self.failed_snapshot()
        self.send(self.event('payment.failed'))
        rows = self.events('payment_failed')
        self.assert_closed(rows, 'payment_failed', canaries=(lava_fx.CONTRACT, lava_fx.PRODUCT, lava_fx.OFFER, '799', 'RUB'))


# --------------------------------------------------------------------------- B14 (interactive events)
class Metrics2PrivacyTests(ShadowHelpers):
    def test_b14_interactive_new_events_carry_only_closed_keys(self):
        self.set_password(email='canary-owner@example.com')
        self.choose()
        self.seed_charts(2)  # 3 account charts
        with self.quiet(), self.sessions() as db:
            chart = NatalChart(**{**fixtures.PAYLOAD, 'city': 'CANARY_BIRTH_CITY'}, user_id=None, session_token=self.guest_token)
            db.add(chart); db.commit(); guest_id = chart.id
        self.seed_answers(10)
        with self.beta():
            response = self.login(guest_id, email='canary-owner@example.com')
            self.assertEqual(response.json()['guest_chart_migration']['status'], 'migrated')
            with patch.object(stripe_billing, 'create_checkout', side_effect=HTTPException(502, 'CANARY provider detail')):
                self.assertEqual(self.client.post('/payments/stripe/checkout', headers={**self.tracked('pricing'), **self.owner}).status_code, 502)
            self.assertEqual(self.ask(question='CANARY_QUESTION про работу').status_code, 200)
            rows = {name: self.events(name) for name in SCHEMAS}
        for name, found in rows.items():
            self.assert_closed(found, name, canaries=('canary', 'chart_id', 'city'))
        self.assertEqual(sorted(row['properties']['resource'] for row in rows['free_limit_would_block']), ['chart', 'question'])


if __name__ == '__main__':
    unittest.main()
