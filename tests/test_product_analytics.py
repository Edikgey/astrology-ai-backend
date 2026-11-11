"""Real API/transactions with an isolated DB; fake PostHog HTTP only."""
import json
import os
import unittest
from datetime import timedelta, datetime
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4
import test_my_charts as fixtures
from database.queries import User, NatalChart, BillingPeriod, Feedback
from modules import product_analytics as analytics
from modules.product_analytics import AnalyticsConsent, AnalyticsOutbox, AnalyticsAccountPreference

ENV = {'POSTHOG_ENABLED': 'true', 'POSTHOG_PRIVACY_VERIFIED': 'true',
       'POSTHOG_EVENT_ENVIRONMENT': 'qa',
       'POSTHOG_HOST': 'https://eu.i.posthog.com', 'POSTHOG_PROJECT_TOKEN': 'phc_fixture_not_a_real_token',
       'POSTHOG_IDENTITY_SECRET': 'isolated-analytics-identity-secret-at-least-32'}


class ProductAnalyticsTests(unittest.TestCase):
    def setUp(self):
        self.off = patch.dict(os.environ, {'POSTHOG_ENABLED': 'false'}); self.off.start()
        fixtures.MyChartsTests.setUp(self)
        self.env = patch.dict(os.environ, ENV); self.env.start()
        analytics._rates.clear()
        while not analytics._facts.empty(): analytics._facts.get_nowait(); analytics._facts.task_done()
        self.visitor, self.revision, self.session = map(str, (uuid4(), uuid4(), uuid4()))
        self.origin = {'Origin': 'https://mylunariaai.com'}

    def tearDown(self):
        self.env.stop()
        analytics.drain_fact_queue(limit=1000)
        fixtures.MyChartsTests.tearDown(self)
        self.off.stop()

    def choose(self, action='accept', owner=None, visitor=None, revision=None):
        return self.client.post('/analytics/consent', headers={**self.origin, **(owner or {})}, json={
            'policy_version': 'posthog-v1', 'visitor_id': visitor or self.visitor,
            'revision': revision or self.revision, 'action': action})

    def record(self, **overrides):
        return dict(event_id=str(uuid4()), session_id=self.session, name='page_viewed',
                    route_key='home', properties={}) | overrides

    def send(self, events=None, owner=None, visitor=None, revision=None, **extra):
        return self.client.post('/analytics/events', headers={**self.origin, **(owner or {}),
            'X-Lunaria-Analytics-Revision': revision or self.revision}, json={
            'policy_version': 'posthog-v1', 'visitor_id': visitor or self.visitor,
            'events': events or [self.record()], **extra})

    def ctx(self, **values):
        return dict(visitor_id=self.visitor, revision=self.revision, session_id=self.session,
                    interactive=True, route_key='chart_creation') | values

    def test_no_consent_no_export_and_no_automatic_guest_downgrade(self):
        self.assertEqual(self.send().status_code, 403)
        self.assertEqual(self.choose().json(), {'accepted': True})
        self.assertEqual(self.send(owner={'Authorization': 'Bearer CANARY_JWT'}).status_code, 401)
        with self.sessions() as db: self.assertEqual(db.query(AnalyticsOutbox).count(), 0)

    def test_accept_creates_only_minimal_hashed_consent_and_valid_event(self):
        self.assertEqual(self.choose().status_code, 200)
        self.assertEqual(self.send().json(), {'accepted': 1})
        with self.sessions() as db:
            consent = db.query(AnalyticsConsent).one(); row = db.query(AnalyticsOutbox).one()
            self.assertEqual(consent.user_id, None); self.assertNotEqual(consent.visitor_key, self.visitor)
            self.assertEqual(len(consent.visitor_key), 64); self.assertTrue(row.distinct_id.startswith('visitor_'))
            self.assertEqual(row.properties['route_key'], 'home')
            self.assertEqual(row.properties['$ip'], None); self.assertTrue(row.properties['$geoip_disable'])

    def test_current_user_is_derived_from_auth_and_other_user_is_rejected(self):
        self.assertTrue(self.choose(owner=self.owner).json()['accepted'])
        self.assertEqual(self.send(owner=self.other).status_code, 403)
        self.assertEqual(self.send(owner=self.owner).status_code, 200)
        self.assertEqual(self.send(owner=self.owner, user_id=2).status_code, 422)
        with self.sessions() as db:
            row = db.query(AnalyticsOutbox).filter_by(event_name='page_viewed').one()
            self.assertEqual(row.user_id, 1); self.assertTrue(row.distinct_id.startswith('account_'))
            self.assertNotIn('a@example.test', json.dumps(row.properties))

    def test_guest_to_account_identify_does_not_merge_other_accounts(self):
        self.choose(); self.send()
        self.assertTrue(self.choose(action='resume', owner=self.owner).json()['accepted'])
        self.assertTrue(self.choose(action='resume', owner=self.other).json()['reset_identity'])
        with self.sessions() as db:
            old = db.query(AnalyticsOutbox).filter_by(event_name='page_viewed').one()
            identity = db.query(AnalyticsOutbox).filter_by(event_name='$identify').one()
            self.assertEqual(identity.properties['$anon_distinct_id'], old.distinct_id)
            self.assertEqual(identity.user_id, 1)

    def test_late_accept_or_events_after_withdrawal_do_not_revive_consent(self):
        self.choose(); event = self.record(); self.send([event])
        self.assertFalse(self.choose(action='withdraw').json()['accepted'])
        self.assertFalse(self.choose(action='accept').json()['accepted'])
        self.assertEqual(self.send([event]).status_code, 403)
        with self.sessions() as db: self.assertEqual(db.query(AnalyticsOutbox).count(), 0)
        self.revision = str(uuid4())
        self.assertTrue(self.choose().json()['accepted']); self.assertEqual(self.send().status_code, 200)

    def test_account_withdrawal_blocks_other_devices_and_backend_facts(self):
        second, rev2 = str(uuid4()), str(uuid4())
        self.choose(owner=self.owner)
        self.choose(owner=self.owner, visitor=second, revision=rev2)
        self.choose(action='withdraw', owner=self.owner)
        self.assertEqual(self.send(owner=self.owner, visitor=second, revision=rev2).status_code, 403)
        self.assertFalse(self.choose(action='resume', owner=self.owner, visitor=second, revision=rev2).json()['accepted'])
        analytics.persist_business(self.engine, 1, 'billing_period_paid', {'provider': 'stripe'}, 'fake-paid', {'interactive': False})
        with self.sessions() as db: self.assertEqual(db.query(AnalyticsOutbox).count(), 0)

    def test_logout_reset_revokes_only_browser_not_account_preference(self):
        self.choose(owner=self.owner)
        self.choose(action='reset')
        with self.sessions() as db:
            self.assertFalse(db.get(AnalyticsConsent, analytics.visitor_key(self.visitor)).accepted)
            self.assertTrue(db.get(AnalyticsAccountPreference, 1).accepted)

    def test_duplicate_delivery_has_one_event_and_retries_keep_uuid(self):
        self.choose(); event = self.record()
        self.assertEqual(self.send([event]).json()['accepted'], 1)
        self.assertEqual(self.send([event]).json()['accepted'], 0)
        calls = []
        def fail(url, **kwargs): calls.append((url, kwargs)); return SimpleNamespace(status_code=503)
        self.assertTrue(analytics.deliver_once(self.sessions, fail))
        with self.sessions() as db:
            row = db.query(AnalyticsOutbox).one(); row.next_attempt_at = analytics.now() - timedelta(seconds=1); db.commit()
        analytics.deliver_once(self.sessions, lambda url, **kwargs: calls.append((url, kwargs)) or SimpleNamespace(status_code=200))
        self.assertEqual(calls[0][1]['json']['uuid'], calls[1][1]['json']['uuid'])
        with self.sessions() as db: self.assertIsNotNone(db.query(AnalyticsOutbox).one().delivered_at)

    def test_egress_never_forwards_url_referrer_tokens_account_or_form_content(self):
        self.choose(owner=self.owner)
        event = self.record(route_key='chart_view')
        self.send([event], owner=self.owner)
        calls = []
        while analytics.deliver_once(self.sessions, lambda url, **kwargs: calls.append((url, kwargs)) or SimpleNamespace(status_code=200)):
            pass
        serialized = json.dumps(calls)
        for marker in ('CANARY', 'example.test', self.owner['Authorization'], 'chart_id', 'birth', 'referrer', '$current_url', 'customer_id'):
            self.assertNotIn(marker, serialized)
        self.assertTrue(all(url == 'https://eu.i.posthog.com/i/v0/e/' for url, _ in calls))
        self.assertTrue(all(options['follow_redirects'] is False for _, options in calls))

    def test_outgoing_attempt_is_not_repeated_after_revocation_or_expiry(self):
        self.choose(); self.send()
        with self.sessions() as db:
            db.query(AnalyticsConsent).one().expires_at = analytics.now() - timedelta(seconds=1); db.commit()
        outgoing = []
        analytics.deliver_once(self.sessions, lambda *a, **k: outgoing.append(k))
        self.assertEqual(outgoing, [])

    def test_client_cannot_forge_financial_facts_or_sensitive_properties(self):
        self.choose()
        for changes in ({'name': 'billing_period_paid', 'properties': {'provider': 'stripe'}},
                        {'properties': {'email': 'CANARY_EMAIL'}}, {'route_key': '/chart/123?token=CANARY'},
                        {'campaign_source': 'CANARY'}, {'viewport_class': 'CANARY'}):
            with self.subTest(changes=changes):
                response = self.send([self.record(**changes)])
                self.assertEqual(response.status_code, 422); self.assertNotIn('CANARY', response.text)

    def test_origin_size_encoding_and_json_fail_closed(self):
        self.choose()
        batch = {'policy_version': 'posthog-v1', 'visitor_id': self.visitor, 'events': [self.record()]}
        for origin in ('https://attacker.example', ''):
            self.assertEqual(self.client.post('/analytics/events', headers={'Origin': origin}, json=batch).status_code, 403)
        h = {**self.origin, 'Content-Type': 'application/json'}
        self.assertEqual(self.client.post('/analytics/events', headers=h, content='x' * 16385).status_code, 413)
        self.assertEqual(self.client.post('/analytics/events', headers=h, content='{CANARY').status_code, 422)
        self.assertEqual(self.client.post('/analytics/events', headers={**h, 'Content-Encoding': 'gzip'}, content='{}').status_code, 415)

    def test_backend_fact_is_emitted_only_after_successful_commit(self):
        self.choose(owner=self.owner)
        marker = analytics.context.set(self.ctx())
        try:
            with self.sessions() as db:
                db.add(NatalChart(user_id=1)); db.flush(); db.rollback()
            analytics.drain_fact_queue()
            with self.sessions() as db:
                self.assertEqual(db.query(AnalyticsOutbox).filter_by(event_name='chart_creation_completed').count(), 0)
                db.add(NatalChart(user_id=1)); db.commit()
            analytics.drain_fact_queue()
            with self.sessions() as db:
                rows = db.query(AnalyticsOutbox).filter_by(event_name='chart_creation_completed').all()
                self.assertEqual(len(rows), 1); self.assertEqual(rows[0].properties['actor'], 'account')
        finally: analytics.context.reset(marker)

    def test_rejected_interactive_device_does_not_use_other_device_consent(self):
        self.choose(owner=self.owner)
        analytics.persist_business(self.engine, 1, 'feedback_submitted', {'category': 'idea'}, 'fake-feedback', {'interactive': True})
        with self.sessions() as db: self.assertEqual(db.query(AnalyticsOutbox).filter_by(event_name='feedback_submitted').count(), 0)

    def test_applied_billing_period_is_server_fact_once_and_no_charge_is_created(self):
        self.choose(owner=self.owner)
        marker = analytics.context.set({'interactive': False})
        try:
            with self.sessions() as db:
                row = BillingPeriod(id='test-period', user_id=1, provider='stripe', account_id='test-account',
                    livemode=False, subscription_id='test-sub', invoice_id='test-invoice',
                    period_start=datetime(2026, 11, 1), period_end=datetime(2026, 12, 1), applied=False)
                db.add(row); db.commit()
                row.applied = True; db.commit()
                row.applied = True; db.commit()
            analytics.drain_fact_queue()
            with self.sessions() as db:
                self.assertEqual(db.query(AnalyticsOutbox).filter_by(event_name='billing_period_paid').count(), 1)
                self.assertEqual(db.query(AnalyticsOutbox).filter_by(event_name='subscription_started').count(), 1)
                self.assertEqual(db.get(User, 1).plan, 'free')
        finally: analytics.context.reset(marker)

    def test_analytics_store_failure_does_not_roll_back_business_write(self):
        self.choose(owner=self.owner)
        marker = analytics.context.set(self.ctx())
        try:
            with patch.object(analytics, 'capture_business', side_effect=RuntimeError('CANARY_INTERNAL_FAILURE')):
                # after_commit must isolate even an unexpected producer failure.
                with self.sessions() as db:
                    db.add(NatalChart(user_id=1)); db.commit()
            with self.sessions() as db: self.assertEqual(db.query(NatalChart).count(), 1)
        finally: analytics.context.reset(marker)

    def test_disabled_configuration_does_not_capture_or_deliver(self):
        with patch.dict(os.environ, {'POSTHOG_PRIVACY_VERIFIED': 'false'}):
            self.assertEqual(self.choose().status_code, 503)
            self.assertFalse(analytics.deliver_once(self.sessions, lambda *a, **k: self.fail('Network must stay off')))

    def test_events_before_new_account_consent_are_not_backfilled(self):
        before = analytics.now() - timedelta(seconds=1)
        self.choose(owner=self.owner)
        analytics.persist_business(self.engine, 1, 'billing_period_paid', {'provider': 'stripe'},
            'paid-before-consent', {'interactive': False}, before)
        with self.sessions() as db:
            self.assertEqual(db.query(AnalyticsOutbox).filter_by(event_name='billing_period_paid').count(), 0)

    def test_resume_does_not_extend_or_revive_expired_consent(self):
        self.choose()
        with self.sessions() as db:
            before = db.query(AnalyticsConsent).one().expires_at
        self.assertTrue(self.choose('resume').json()['accepted'])
        with self.sessions() as db:
            row = db.query(AnalyticsConsent).one(); self.assertEqual(row.expires_at, before)
            row.expires_at = analytics.now() - timedelta(seconds=1); db.commit()
        self.assertFalse(self.choose('resume').json()['accepted'])

    def test_nested_commit_followed_by_outer_rollback_is_not_a_success(self):
        self.choose(owner=self.owner)
        marker = analytics.context.set(self.ctx())
        try:
            with self.sessions() as db:
                db.add(NatalChart(user_id=1)); db.flush()
                with db.begin_nested():
                    db.add(NatalChart(user_id=1)); db.flush()
                db.rollback()
            analytics.drain_fact_queue()
            with self.sessions() as db:
                self.assertEqual(db.query(AnalyticsOutbox).filter_by(event_name='chart_creation_completed').count(), 0)
        finally: analytics.context.reset(marker)

    def test_actual_http_chart_request_uses_only_explicit_analytics_headers(self):
        self.choose(owner=self.owner)
        headers = {**self.owner, **self.origin, 'X-Lunaria-Analytics-Visitor': self.visitor,
            'X-Lunaria-Analytics-Revision': self.revision, 'X-Lunaria-Analytics-Session': self.session,
            'X-Lunaria-Analytics-Page': 'chart_creation', 'Referer': 'https://mylunariaai.com/?token=CANARY'}
        with fixtures.MyChartsTests.mock_calculations(self):
            response = self.client.post('/natal-chart?token=CANARY', headers=headers,
                json={**fixtures.PAYLOAD, 'city': 'CANARY_BIRTH_CITY'})
        self.assertEqual(response.status_code, 200, response.text)
        analytics.drain_fact_queue()
        with self.sessions() as db:
            row = db.query(AnalyticsOutbox).filter_by(event_name='chart_creation_completed').one()
            self.assertEqual(row.properties['route_key'], 'chart_creation')
            self.assertNotIn('CANARY', json.dumps(row.properties))


if __name__ == '__main__': unittest.main()
