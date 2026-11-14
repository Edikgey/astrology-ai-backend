"""PRICING-1 regression, backend (disposable PostgreSQL part): BE-3, BE-4 concurrency, BE-14 migration rehearsal.

NEVER uses DATABASE_URL: USAGE_TEST_POSTGRES_URL must name a loopback database test_* / *_test (set by
scripts/test-backend.ps1, which creates and deletes it). Each test owns and drops a random schema.

The schema is the 393158f schema rebuilt here (same chain as test_postgres_usage, minus every PRICING-1
addition), so the rehearsal applies migrations/pricing_packs.sql to exactly what production has today and does
not depend on how the author updates the shared fixture. Interleavings are forced, not hoped for: a holder
session keeps the user row lock until both workers are observed blocked on it (pg_blocking_pids), or a Barrier
inside the fake provider read makes both deliveries pass their pre-checks before either commits.
"""
import copy
import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from threading import Barrier, Event
from time import monotonic, sleep
from unittest.mock import patch
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import MetaData, Table, create_engine, inspect, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

import test_my_charts as fixtures  # isolated imports, dummy OpenAI key, no network
import test_stripe as sf
import test_pricing as pf
from database.queries import BillingEvent, GPTUsage
from modules import stripe_billing, usage

MIGRATIONS = Path(__file__).parents[1] / 'migrations'
CHAIN = ('plan_usage.sql', 'paddle_billing.sql', 'lava_billing.sql', 'billing_conflicts.sql', 'ai_conversation.sql',
         'birth_time_houses.sql', 'relationships.sql', 'stripe_billing.sql')  # as test_postgres_usage.setUp
SQL_TABLES = {'analytics_consents', 'analytics_account_preferences', 'analytics_outbox', 'analytics_monthly_budget', 'feedback',
              'stripe_checkouts', 'billing_customers', 'billing_periods', 'billing_events', 'billing_conflicts', 'gpt_usage',
              'paddle_checkouts', 'paddle_events', 'lava_checkouts', 'gpt_conversations', 'relationships',
              'relationship_messages', 'relationship_conversations'}
PRICING_TABLES = {'question_packs'}
LEGACY_USER_COLUMNS = {'plan', 'current_period_start', 'current_period_end', 'payment_provider', 'provider_customer_id',
                       'provider_subscription_id', 'subscription_status', 'paddle_updated_at', 'scheduled_cancel_at'}
PRICING_USER_COLUMNS = {'current_period_interval'}


@unittest.skipUnless(os.getenv('USAGE_TEST_POSTGRES_URL'), 'Local PostgreSQL test URL not configured')
class PricingPostgresBase(unittest.TestCase):
    APPLY_PRICING = True

    def setUp(self):
        url = make_url(os.environ['USAGE_TEST_POSTGRES_URL'])
        if (url.get_backend_name() != 'postgresql' or url.host not in ('localhost', '127.0.0.1', '::1') or
                not ((url.database or '').startswith('test_') or (url.database or '').endswith('_test'))):
            raise RuntimeError('Only an explicitly named local test database is allowed')
        self.schema = 'pricing_test_' + uuid4().hex
        self.admin = create_engine(url)
        with self.admin.begin() as conn:
            conn.exec_driver_sql(f'CREATE SCHEMA "{self.schema}"')
        self.addCleanup(self.cleanup_schema)
        self.engine = create_engine(url, connect_args={'options': f'-csearch_path={self.schema}'})
        self.sessions = sessionmaker(bind=self.engine, autoflush=False)
        legacy = MetaData()
        for table in fixtures.Base.metadata.sorted_tables:
            if table.name in SQL_TABLES | PRICING_TABLES:
                continue
            if table.name == 'users':
                Table('users', legacy, *(c._copy() for c in table.columns if c.name not in LEGACY_USER_COLUMNS | PRICING_USER_COLUMNS))
            elif table.name in ('natal_charts', 'chart_data'):
                Table(table.name, legacy, *(c._copy() for c in table.columns if c.name not in ('timezone', 'birth_utc', 'houses', 'house_system')))
            else:
                table.to_metadata(legacy)
        for index in list(legacy.tables['gpt_messages'].indexes):
            if index.name == 'ix_gpt_messages_chart_id_id':
                legacy.tables['gpt_messages'].indexes.remove(index)
        legacy.create_all(self.engine)
        chart = {k: v for k, v in fixtures.PAYLOAD.items() if k != 'timezone'}
        with self.engine.begin() as conn:
            conn.execute(legacy.tables['users'].insert(), [{'id': 1, 'email': 'a@example.test', 'password_hash': 'unused'},
                                                           {'id': 2, 'email': 'b@example.test', 'password_hash': 'unused'}])
            conn.execute(legacy.tables['natal_charts'].insert(), [{'id': 1, 'user_id': 1, **chart}, {'id': 2, 'user_id': 2, **chart},
                                                                  {'id': 4, 'user_id': 1, **chart}])
            conn.execute(legacy.tables['gpt_messages'].insert(), [{'chart_id': 1, 'role': role, 'content': 'Historical ' + role}
                                                                  for role in ('user', 'gpt')])
        for name in CHAIN:
            self.apply_script((MIGRATIONS / name).read_text(encoding='utf-8'))
        if self.APPLY_PRICING:
            self.apply_script(self.pricing_script())

    def pricing_script(self):
        return (MIGRATIONS / 'pricing_packs.sql').read_text(encoding='utf-8')

    def apply_script(self, script):
        raw = self.engine.raw_connection()
        try:
            self.assertFalse(raw.driver_connection.autocommit)
            with raw.cursor() as cursor:
                cursor.execute(script)
            raw.commit()
        except BaseException:
            raw.rollback()
            raise
        finally:
            raw.close()

    def cleanup_schema(self):
        if hasattr(self, 'engine'):
            self.engine.dispose()
        with self.admin.begin() as conn:  # generated name, never from input
            conn.exec_driver_sql(f'DROP SCHEMA "{self.schema}" CASCADE')
        self.admin.dispose()

    def table(self, name):
        return Table(name, MetaData(), autoload_with=self.engine)

    def seed_pack(self, user_id=1, quantity=pf.PACK_SIZE, state='paid', session_id=None, pi=None, customer=sf.CUSTOMER):
        pack_id = str(uuid4())
        now = datetime.utcnow()
        with self.engine.begin() as conn:
            conn.execute(self.table('question_packs').insert().values(
                id=pack_id, user_id=user_id, provider='stripe', account_id=sf.ACCOUNT, livemode=True, quantity=quantity,
                state=state, customer_id=customer, session_id=session_id or 'cs_live_seed' + pack_id[:8],
                payment_intent_id=pi, created_at=now - timedelta(minutes=5), paid_at=now - timedelta(minutes=1) if state == 'paid' else None))
        return pack_id

    def pack(self, pack_id):
        packs = self.table('question_packs')
        with self.engine.connect() as conn:
            return dict(conn.execute(select(packs).where(packs.c.id == pack_id)).mappings().one())

    def scalar(self, sql, **params):
        with self.engine.connect() as conn:
            return conn.execute(text(sql), params).scalar_one()

    def wait_blocked(self, pids, holder_pid):
        deadline, blocked = monotonic() + 10, set()
        while monotonic() < deadline:
            with self.engine.connect() as observer:
                blocked = {pid for pid in pids if holder_pid in observer.execute(text('SELECT pg_blocking_pids(:p)'), {'p': pid}).scalar_one()}
            if blocked == set(pids):
                return
            sleep(0.02)
        self.fail(f'workers not blocked on the user lock: {sorted(blocked)} of {sorted(pids)}')


# =========================================================================== BE-3
class PricingBE3LastPackQuestionTests(PricingPostgresBase):
    def test_be3_last_pack_question_two_connections_one_wins(self):
        with self.sessions() as db:  # free 10/10 (one historical question is backfilled by plan_usage.sql)
            db.add_all([GPTUsage(user_id=1, chart_id=1, status='succeeded', plan='free') for _ in range(9)])
            db.commit()
        pack_id = self.seed_pack(quantity=1)
        pids, ready = {}, Event()

        def attempt(chart_id):
            with self.sessions() as db:
                pids[chart_id] = db.execute(text('SELECT pg_backend_pid()')).scalar_one()
                if len(pids) == 2:
                    ready.set()
                try:
                    return usage.reserve(db, 1, chart_id)
                except HTTPException as error:
                    return error
                finally:
                    self.assertFalse(db.in_transaction())

        with self.sessions() as holder, ThreadPoolExecutor(max_workers=2) as pool:
            usage.locked_user(holder, 1)
            holder_pid = holder.execute(text('SELECT pg_backend_pid()')).scalar_one()
            futures = [pool.submit(attempt, chart_id) for chart_id in (1, 4)]  # two charts: no GPT_REQUEST_IN_PROGRESS
            try:
                self.assertTrue(ready.wait(10))
                self.wait_blocked(list(pids.values()), holder_pid)
            finally:
                holder.rollback()
            results = [f.result(timeout=20) for f in futures]
        winners = [r for r in results if not isinstance(r, HTTPException)]
        losers = [r for r in results if isinstance(r, HTTPException)]
        self.assertEqual((len(winners), len(losers)), (1, 1), results)
        self.assertEqual((losers[0].status_code, losers[0].detail['code']), (409, 'GPT_LIMIT_REACHED'))
        with self.sessions() as db:
            usage.finalize(db, 1, winners[0], 'Вопрос', 'Ответ')
        self.assertEqual(self.scalar("SELECT count(*) FROM gpt_usage WHERE pack_id = :p AND status = 'succeeded'", p=pack_id), 1)
        with self.sessions() as db:
            self.assertEqual(usage.account_usage(db, 1)['pack_questions_remaining'], 0)


# =========================================================================== BE-4 (concurrent deliveries)
class PricingBE4ConcurrentGrantTests(PricingPostgresBase):
    def setUp(self):
        super().setUp()
        env = patch.dict(os.environ, pf.ENV); env.start(); self.addCleanup(env.stop)
        pf.install_fakes(self, True)
        self.pack_id = self.seed_pack(state='ready', session_id='cs_live_pack')
        self.remote['cs_live_pack'] = {
            'id': 'cs_live_pack', 'object': 'checkout.session', 'livemode': True, 'status': 'complete', 'mode': 'payment',
            'payment_status': 'paid', 'customer': sf.CUSTOMER, 'payment_intent': 'pi_pack', 'amount_total': pf.STRIPE_PACK_CENTS,
            'currency': 'usd', 'metadata': {'pack_id': self.pack_id, 'user_id': '1'}, 'client_reference_id': self.pack_id,
            'subscription': None, 'line_items': {'object': 'list', 'has_more': False, 'data': [{'id': 'li_pack', 'object': 'item',
                'price': copy.deepcopy(self.prices[pf.PACK_PRICE]), 'quantity': 1, 'amount_total': pf.STRIPE_PACK_CENTS, 'currency': 'usd'}]}}
        self.intents['pi_pack'] = {'id': 'pi_pack', 'object': 'payment_intent', 'livemode': True, 'status': 'succeeded',
            'amount': pf.STRIPE_PACK_CENTS, 'amount_received': pf.STRIPE_PACK_CENTS, 'currency': 'usd', 'customer': sf.CUSTOMER,
            'metadata': {'pack_id': self.pack_id, 'user_id': '1'}}

    def deliver_together(self, events):
        """Both deliveries read the provider (outside any lock) before either may lock and commit."""
        barrier = Barrier(len(events))
        original = self.api.checkout.sessions.retrieve.side_effect

        def read(session_id, *a, **k):
            barrier.wait(timeout=10)  # BrokenBarrierError = a provider read under the user lock (stripe_billing.py:3)
            return original(session_id, *a, **k)

        def run(value):
            with self.sessions() as db:
                try:
                    return stripe_billing.process_event(db, value)
                except HTTPException as error:
                    return error
        with patch.object(self.api.checkout.sessions, 'retrieve', side_effect=read), ThreadPoolExecutor(max_workers=len(events)) as pool:
            return [f.result(timeout=30) for f in [pool.submit(run, value) for value in events]]

    def assert_one_grant(self, results, expected_events):
        self.assertFalse([r for r in results if isinstance(r, HTTPException)], results)
        self.assertEqual(self.pack(self.pack_id)['state'], 'paid')
        self.assertEqual(self.scalar('SELECT count(*) FROM question_packs'), 1)
        self.assertEqual(self.scalar('SELECT count(*) FROM billing_events'), expected_events)
        with self.sessions() as db:
            self.assertEqual(usage.account_usage(db, 1)['pack_questions_remaining'], pf.PACK_SIZE)
            user = db.get(fixtures.User, 1)
            self.assertEqual((user.plan, user.payment_provider), ('free', None))

    def test_be4_same_event_delivered_twice_at_once_grants_once(self):
        value = sf.event('checkout.session.completed', 'cs_live_pack')
        results = self.deliver_together([value, copy.deepcopy(value)])
        self.assertIn('duplicate', results)
        self.assert_one_grant(results, 1)

    def test_be4_completed_and_async_succeeded_at_once_grant_once(self):
        results = self.deliver_together([sf.event('checkout.session.completed', 'cs_live_pack'),
                                         sf.event('checkout.session.async_payment_succeeded', 'cs_live_pack')])
        self.assert_one_grant(results, 2)


# =========================================================================== BE-14 — migration rehearsal on the restored schema
class PricingBE14MigrationTests(PricingPostgresBase):
    APPLY_PRICING = False

    def setUp(self):
        super().setUp()
        with self.engine.begin() as conn:
            conn.exec_driver_sql("INSERT INTO stripe_checkouts (id, user_id, account_id, livemode, state, created_at) "
                                 "VALUES ('old-stripe', 1, 'acct_x', true, 'ready', now())")
            conn.exec_driver_sql("INSERT INTO lava_checkouts (id, user_id, offer_id, product_id, buyer_email, state, created_at) "
                                 "VALUES ('old-lava', 1, 'offer', 'product', 'a@example.test', 'ready', now())")
            conn.exec_driver_sql("UPDATE users SET plan = 'premium', current_period_start = now() - interval '1 day', "
                                 "current_period_end = now() + interval '29 days' WHERE id = 2")
        self.before = self.snapshot()

    def snapshot(self):
        with self.engine.connect() as conn:
            tables = inspect(conn).get_table_names()
            return {name: sorted(map(repr, conn.exec_driver_sql(f'SELECT * FROM "{name}"').all())) for name in sorted(tables)}

    def columns(self, table):
        with self.engine.connect() as conn:
            return {c['name'] for c in inspect(conn).get_columns(table)}

    def assert_untouched(self):
        self.assertEqual(self.snapshot(), self.before)
        with self.engine.connect() as conn:
            self.assertNotIn('question_packs', inspect(conn).get_table_names())
        self.assertNotIn('current_period_interval', self.columns('users'))
        self.assertNotIn('pack_id', self.columns('gpt_usage'))
        self.assertNotIn('price_id', self.columns('stripe_checkouts'))
        self.assertNotIn('periodicity', self.columns('lava_checkouts'))

    def test_be14_script_is_one_bounded_transaction(self):
        script = self.pricing_script()
        self.assertTrue(script.lstrip().upper().startswith(('BEGIN', '--')))
        for needle in ('BEGIN;', 'COMMIT;', 'lock_timeout', 'statement_timeout'):
            self.assertIn(needle, script)
        self.assertNotIn('IF NOT EXISTS', script.upper())

    def test_be14_failure_before_commit_changes_nothing(self):
        broken = self.pricing_script().replace('COMMIT;', 'SELECT 1 / 0;\nCOMMIT;')
        with self.assertRaises(Exception) as error:
            self.apply_script(broken)
        self.assertEqual(error.exception.pgcode, '22012')
        self.assert_untouched()

    def test_be14_additive_with_defaults_for_old_rows(self):
        self.apply_script(self.pricing_script())
        with self.engine.connect() as conn:
            self.assertEqual(conn.exec_driver_sql('SELECT DISTINCT current_period_interval FROM users').scalars().all(), [None])
            self.assertEqual(conn.exec_driver_sql("SELECT price_id FROM stripe_checkouts WHERE id = 'old-stripe'").scalar_one(), None)
            self.assertEqual(conn.exec_driver_sql("SELECT periodicity FROM lava_checkouts WHERE id = 'old-lava'").scalar_one(), 'MONTHLY')
            self.assertEqual(conn.exec_driver_sql('SELECT count(*) FROM gpt_usage WHERE pack_id IS NOT NULL').scalar_one(), 0)
            self.assertEqual(conn.exec_driver_sql('SELECT count(*) FROM question_packs').scalar_one(), 0)
        after = self.snapshot()
        for name, rows in self.before.items():
            self.assertEqual(len(after[name]), len(rows), name)  # nothing deleted or added in old tables
        with self.sessions() as db:  # new code reads the migrated data
            self.assertEqual(usage.account_usage(db, 1)['gpt_messages_used'], 1)
            self.assertEqual(usage.account_usage(db, 2)['plan'], 'premium')

    def test_be14_repeat_application_fails_without_changes(self):
        self.apply_script(self.pricing_script())
        migrated = self.snapshot()
        with self.assertRaises(Exception):
            self.apply_script(self.pricing_script())
        self.assertEqual(self.snapshot(), migrated)

    def test_be14_393158f_writes_still_work_on_the_new_schema(self):
        self.apply_script(self.pricing_script())
        with self.engine.begin() as conn:  # the exact column sets the 393158f models write
            conn.exec_driver_sql("INSERT INTO lava_checkouts (id, user_id, offer_id, product_id, buyer_email, state, created_at, cancel_requested) "
                                 "VALUES ('old-code-lava', 2, 'offer', 'product', 'b@example.test', 'creating', now(), false)")
            conn.exec_driver_sql("INSERT INTO stripe_checkouts (id, user_id, account_id, livemode, state, created_at, revision) "
                                 "VALUES ('old-code-stripe', 2, 'acct_x', true, 'creating', now(), 0)")
            conn.exec_driver_sql("INSERT INTO gpt_usage (id, user_id, chart_id, status, plan, created_at) "
                                 "VALUES ('" + str(uuid4()) + "', 2, 2, 'succeeded', 'free', now())")
            conn.exec_driver_sql("UPDATE users SET plan = 'free', current_period_start = NULL, current_period_end = NULL WHERE id = 2")
            self.assertEqual(conn.exec_driver_sql("SELECT periodicity FROM lava_checkouts WHERE id = 'old-code-lava'").scalar_one(), 'MONTHLY')
            self.assertEqual(conn.exec_driver_sql("SELECT price_id FROM stripe_checkouts WHERE id = 'old-code-stripe'").scalar_one(), None)

    def test_be14_constraints_hold(self):
        self.apply_script(self.pricing_script())
        pack = self.seed_pack(pi='pi_unique', session_id='cs_live_unique')
        bad = [("UPDATE users SET current_period_interval = 'week' WHERE id = 1", '23514'),
               ("UPDATE lava_checkouts SET periodicity = 'WEEKLY' WHERE id = 'old-lava'", '23514'),
               (f"UPDATE question_packs SET state = 'granted' WHERE id = '{pack}'", '23514'),
               (f"UPDATE question_packs SET provider = 'paypal' WHERE id = '{pack}'", '23514'),
               ("INSERT INTO question_packs (id, user_id, provider, account_id, livemode, quantity, state, session_id, created_at) "
                "VALUES ('dup-session', 1, 'stripe', 'acct_isolated', true, 50, 'ready', 'cs_live_unique', now())", '23505'),
               ("INSERT INTO question_packs (id, user_id, provider, account_id, livemode, quantity, state, payment_intent_id, created_at) "
                "VALUES ('dup-intent', 1, 'stripe', 'acct_isolated', true, 50, 'ready', 'pi_unique', now())", '23505'),
               ("UPDATE gpt_usage SET pack_id = 'missing-pack'", '23503'),
               ('DELETE FROM users WHERE id = 1', '23503')]  # FK without cascade: packs never disappear with a user
        for sql, code in bad:
            with self.subTest(sql=sql[:60]):
                with self.assertRaises(Exception) as error, self.engine.begin() as conn:
                    conn.exec_driver_sql(sql)
                self.assertEqual(getattr(error.exception.orig, 'pgcode', None), code)
        self.assertEqual(self.pack(pack)['state'], 'paid')

    def test_be14_rollback_refuses_when_packs_or_years_exist_then_reverts_clean(self):
        rollback = (MIGRATIONS / 'pricing_packs_rollback.sql').read_text(encoding='utf-8')
        self.apply_script(self.pricing_script())
        pack = self.seed_pack()
        with self.assertRaises(Exception):
            self.apply_script(rollback)
        self.assertEqual(self.pack(pack)['state'], 'paid')
        with self.engine.begin() as conn:
            conn.exec_driver_sql('DELETE FROM question_packs')
            conn.exec_driver_sql("UPDATE users SET current_period_interval = 'year' WHERE id = 2")
        with self.assertRaises(Exception):
            self.apply_script(rollback)
        self.assertIn('current_period_interval', self.columns('users'))
        with self.engine.begin() as conn:
            conn.exec_driver_sql("UPDATE users SET current_period_interval = NULL WHERE id = 2")
            conn.exec_driver_sql("UPDATE lava_checkouts SET periodicity = 'PERIOD_YEAR' WHERE id = 'old-lava'")
        with self.assertRaises(Exception):
            self.apply_script(rollback)
        with self.engine.begin() as conn:
            conn.exec_driver_sql("UPDATE lava_checkouts SET periodicity = 'MONTHLY' WHERE id = 'old-lava'")
        self.apply_script(rollback)
        self.assert_untouched()


if __name__ == '__main__':
    unittest.main()
