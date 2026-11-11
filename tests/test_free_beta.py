"""Free beta product entitlements, isolated accounts/provider mocks only."""
import os
import unittest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch
from fastapi import HTTPException
import test_my_charts as fixtures
import test_usage as quota_fixtures
import test_postgres_usage as pg_fixtures
import test_relationship_ai as relationship_fixtures
from database.queries import User, NatalChart, GPTUsage
from modules import usage
from modules.plans import plan_limits
from modules.free_beta import free_beta_enabled
from modules.migration import migrate_guest_data_to_user

FLAG = 'LUNARIA_FREE_BETA_ENABLED'

class FreeBetaTests(unittest.TestCase):
    chart = fixtures.MyChartsTests.chart
    mock_calculations = fixtures.MyChartsTests.mock_calculations
    state = quota_fixtures.UsageTests.state
    seed_usage = quota_fixtures.UsageTests.seed_usage
    ask = quota_fixtures.UsageTests.ask

    def setUp(self):
        fixtures.MyChartsTests.setUp(self)
        self.flag = patch.dict(os.environ, {FLAG: 'true'})
        self.flag.start()
        self.addCleanup(self.flag.stop)
    tearDown = fixtures.MyChartsTests.tearDown

    def test_flag_requires_explicit_server_opt_in(self):
        for value in ('', 'false', '1', 'yes', 'false,true'):
            with self.subTest(value=value), patch.dict(os.environ, {FLAG: value}):
                self.assertFalse(free_beta_enabled())
        with patch.dict(os.environ, {FLAG: 'TRUE'}):
            self.assertTrue(free_beta_enabled())

    def test_public_config_is_safe_uncached_and_client_cannot_enable_beta(self):
        response = self.client.get('/public-config', headers={'Origin':'https://mylunariaai.com'})
        self.assertEqual(response.json(), {'free_beta_enabled':True})
        self.assertEqual(response.headers['cache-control'], 'no-store')
        self.assertEqual(response.headers['access-control-allow-origin'], 'https://mylunariaai.com')
        with patch.dict(os.environ, {FLAG: ''}):
            self.assertEqual(self.client.get('/public-config?free_beta_enabled=true', headers={FLAG:'true'}).json(), {'free_beta_enabled':False})
            state = self.state()
            self.assertEqual((state['saved_charts_limit'],state['gpt_messages_limit']), (3,10))

    def test_beta_usage_is_explicitly_unlimited_without_a_paid_entitlement(self):
        state = self.state()
        self.assertEqual(state['plan'],'free')
        self.assertTrue(state['free_beta_enabled'])
        for key in ('saved_charts_limit','gpt_messages_limit','gpt_messages_available'):
            self.assertIsNone(state[key])
        self.assertEqual(state['gpt_limit_type'],'unlimited')
        with self.sessions() as db:
            user=db.get(User,1)
            self.assertEqual(user.plan,'free')
            self.assertIsNone(user.current_period_start)
            self.assertIsNone(user.provider_subscription_id)

    def test_authenticated_chart_creation_exceeds_both_normal_plan_limits(self):
        with self.mock_calculations():
            for _ in range(11):
                response=self.client.post('/natal-chart',json=fixtures.PAYLOAD,headers=self.owner)
                self.assertEqual(response.status_code,200,response.text)
        result=self.client.get('/natal-charts',headers=self.owner).json()
        self.assertEqual(result['count'],11)
        self.assertIsNone(result['limit'])

    def test_chat_exceeds_ten_and_three_hundred_while_accounting_is_preserved(self):
        chart_id=self.chart(related=False)
        self.seed_usage(301)
        with patch('api.endpoints.ChartInterpreter') as provider:
            provider.return_value.ask_gpt=AsyncMock(return_value='Synthetic beta answer')
            response=self.ask(chart_id)
            self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(self.state()['gpt_messages_used'],302)
        with self.sessions() as db:
            self.assertEqual(db.query(GPTUsage).filter_by(status='succeeded').count(),302)
            self.assertEqual(db.get(User,1).plan,'free')

    def test_expired_premium_period_does_not_block_beta_or_change_subscription(self):
        chart_id=self.chart(related=False)
        start=datetime.utcnow()-timedelta(days=32);end=start+timedelta(days=30)
        with self.sessions() as db:
            user=db.get(User,1);user.plan='premium';user.current_period_start=start;user.current_period_end=end
            user.payment_provider='stripe';user.provider_subscription_id='sub_isolated_fixture';user.subscription_status='past_due';db.commit()
        with self.sessions() as db:
            reservation=usage.reserve(db,1,chart_id)
            usage.release(db,1,reservation)
        self.assertTrue(self.state()['gpt_period_valid'])
        with self.sessions() as db:
            user=db.get(User,1)
            self.assertEqual((user.plan,user.current_period_start,user.current_period_end,user.provider_subscription_id,user.subscription_status),('premium',start,end,'sub_isolated_fixture','past_due'))
        with patch.dict(os.environ,{FLAG:'false'}), self.sessions() as db:
            with self.assertRaises(HTTPException) as caught:usage.reserve(db,1,chart_id)
            self.assertEqual(caught.exception.detail['code'],'GPT_PERIOD_INVALID')

    def test_inflight_ownership_and_authentication_guards_remain(self):
        chart_id=self.chart(related=False)
        foreign=self.chart(user_id=2,related=False)
        with self.sessions() as db:
            reservation=usage.reserve(db,1,chart_id)
            with self.assertRaises(HTTPException) as caught:usage.reserve(db,1,chart_id)
            self.assertEqual(caught.exception.detail['code'],'GPT_REQUEST_IN_PROGRESS')
            usage.release(db,1,reservation)
        self.assertEqual(self.ask(foreign).status_code,403)
        self.assertEqual(self.client.post('/ask-gpt',json={'chart_id':chart_id,'question':'Question'}).status_code,401)
        self.assertEqual(self.client.get('/account/usage',headers=self.guest).status_code,401)

    def test_guest_transfer_exceeds_chart_quota_but_still_checks_guest_ownership(self):
        for _ in range(4):self.chart(related=False)
        guest=self.chart(user_id=None,session_token=self.guest_token,related=False)
        with self.sessions() as db:
            migrated=migrate_guest_data_to_user(db,1,self.guest_token,guest)
            db.commit()
            self.assertEqual(migrated['status'],'migrated')
            self.assertEqual(db.get(NatalChart,guest).user_id,1)
        other=self.chart(user_id=None,session_token=fixtures.uuid4(),related=False)
        with self.sessions() as db:
            self.assertEqual(migrate_guest_data_to_user(db,1,self.guest_token,other)['status'],'not_found')

    def test_beta_off_restores_quotas_without_deleting_charts_or_resetting_usage(self):
        for _ in range(4):self.chart(related=False)
        self.seed_usage(11)
        self.assertIsNone(self.state()['gpt_messages_available'])
        with patch.dict(os.environ,{FLAG:'false'}):
            state=self.state()
            self.assertFalse(state['free_beta_enabled'])
            self.assertEqual((state['saved_charts_used'],state['saved_charts_limit'],state['gpt_messages_used'],state['gpt_messages_limit'],state['gpt_messages_available']),(4,3,11,10,0))
            self.assertEqual(self.client.get('/natal-charts',headers=self.owner).json()['count'],4)
            self.assertEqual(self.client.post('/natal-chart',json=fixtures.PAYLOAD,headers=self.owner).status_code,409)

    def test_relationship_chat_uses_the_same_unlimited_beta_and_ledger(self):
        rid, _ = relationship_fixtures.RelationshipAITests.pair(self)
        self.seed_usage(11)
        with patch('modules.interpretation.client.chat.completions.create', return_value=relationship_fixtures.reply()) as provider:
            response=relationship_fixtures.RelationshipAITests.ask(self,rid)
            self.assertEqual(response.status_code,200,response.text)
            provider.assert_called_once()
        with self.sessions() as db:
            self.assertEqual(db.query(GPTUsage).filter_by(relationship_id=rid,status='succeeded').count(),1)

    def test_unknown_plan_still_fails_closed(self):
        from types import SimpleNamespace
        with self.assertRaises(HTTPException):plan_limits(SimpleNamespace(plan='arbitrary'))

    def test_checkout_remains_available_and_provider_flow_is_unchanged(self):
        for provider,module in [('stripe','stripe_billing'),('lava','lava_billing')]:
            with self.subTest(provider=provider),patch(f'modules.{module}.create_checkout',return_value={'synthetic':True}) as create:
                response=self.client.post(f'/payments/{provider}/checkout',json={},headers=self.owner)
                self.assertEqual(response.status_code,200,response.text)
                create.assert_called_once()

@unittest.skipUnless(os.getenv('USAGE_TEST_POSTGRES_URL'),'Local PostgreSQL test URL not configured')
class FreeBetaPostgreSQLTests(unittest.TestCase):
    def test_real_postgres_beta_reservation_and_normal_rollback(self):
        pg=pg_fixtures.PostgreSQLUsageTests('test_birth_migration_preserves_legacy_data_without_inventing_timezone')
        pg.setUp();self.addCleanup(pg.doCleanups)
        with patch.dict(os.environ,{FLAG:'true'}),pg.sessions() as db:
            user=usage.locked_user(db,1)
            self.assertIsNone(usage.quota_state(db,user,usage.utcnow())['gpt_messages_limit'])
            reservation=usage.reserve(db,1,1)
            usage.finalize(db,1,reservation,'Synthetic beta question','Synthetic answer')
            self.assertEqual(db.get(GPTUsage,reservation).status,'succeeded')
            self.assertEqual(db.get(User,1).plan,'free')
        with patch.dict(os.environ,{FLAG:'false'}),pg.sessions() as db:
            state=usage.account_usage(db,1)
            self.assertEqual(state['gpt_messages_limit'],10)
            self.assertGreaterEqual(state['gpt_messages_used'],1)
