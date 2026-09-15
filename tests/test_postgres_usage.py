"""Opt-in real PostgreSQL migration/locking tests; NEVER uses DATABASE_URL.

Set USAGE_TEST_POSTGRES_URL to a localhost database named test_* or *_test.
Each test uses/drops only its own randomly named schema. Default run skips it.
"""
import os
import unittest
from pathlib import Path
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event
from time import monotonic, sleep
from uuid import uuid4
from sqlalchemy import create_engine, MetaData, Table, text, inspect
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker
from fastapi import HTTPException
import test_my_charts as fixtures  # isolated imports, dummy OpenAI key, no network
from database.queries import User, GPTMessage, GPTUsage, GPTConversation
from modules import usage


@unittest.skipUnless(os.getenv("USAGE_TEST_POSTGRES_URL"), "Local PostgreSQL test URL not configured")
class PostgreSQLUsageTests(unittest.TestCase):
    def setUp(self):
        url = make_url(os.environ["USAGE_TEST_POSTGRES_URL"])
        if (url.get_backend_name() != "postgresql" or url.host not in ("localhost", "127.0.0.1", "::1") or
                not ((url.database or "").startswith("test_") or (url.database or "").endswith("_test"))):
            raise RuntimeError("Only an explicitly named local test database is allowed")
        self.schema = "usage_test_" + uuid4().hex
        self.admin = create_engine(url)
        with self.admin.begin() as conn:
            conn.exec_driver_sql(f'CREATE SCHEMA "{self.schema}"')
        self.addCleanup(self.cleanup_schema)
        self.engine = create_engine(url, connect_args={"options": f"-csearch_path={self.schema}"})
        self.sessions = sessionmaker(bind=self.engine, autoflush=False)
        # Reconstruct the repository's pre-feature schema, then run the exact SQL.
        legacy = MetaData()
        for table in fixtures.Base.metadata.sorted_tables:
            if table.name in ("gpt_usage", "paddle_checkouts", "paddle_events", "gpt_conversations", "relationships",
                              "relationship_messages", "relationship_conversations"):
                continue
            if table.name == "users":
                Table("users", legacy, *(col._copy() for col in table.columns if col.name not in
                                          ("plan", "current_period_start", "current_period_end", "payment_provider",
                                           "provider_customer_id", "provider_subscription_id", "subscription_status",
                                           "paddle_updated_at", "scheduled_cancel_at")))
            elif table.name in ('natal_charts', 'chart_data'):
                Table(table.name, legacy, *(col._copy() for col in table.columns if col.name not in
                    ('timezone','birth_utc','houses','house_system')))
            else:
                table.to_metadata(legacy)
        for index in list(legacy.tables['gpt_messages'].indexes):
            if index.name == 'ix_gpt_messages_chart_id_id':
                legacy.tables['gpt_messages'].indexes.remove(index)
        legacy.create_all(self.engine)
        self.legacy = legacy
        with self.engine.begin() as conn:
            conn.execute(legacy.tables["users"].insert(), [
                {"id": 1, "email": "test@example.com", "password_hash": "unused"},
                {"id": 2, "email": "other@example.com", "password_hash": "unused"},
            ])
            conn.execute(legacy.tables["natal_charts"].insert(), [
                {"id": 1, "user_id": 1, **{k:v for k,v in fixtures.PAYLOAD.items() if k != "timezone"}},
                {"id": 2, "user_id": 2, **{k:v for k,v in fixtures.PAYLOAD.items() if k != "timezone"}},
                {"id": 3, "user_id": None, **{k:v for k,v in fixtures.PAYLOAD.items() if k != "timezone"}},
            ])
            conn.execute(legacy.tables["gpt_messages"].insert(), [
                {"chart_id": chart_id, "role": role, "content": f"Historical {role} {chart_id}"}
                for chart_id in (1, 2, 3) for role in ("user", "gpt")
            ])
            conn.execute(legacy.tables["chart_data"].insert(), {
                "chart_id": 1, "bodies_for_circle": fixtures.BODY, "aspects_for_circle": [],
                "points_data": [], "patterns_data": [], "aspects_structured": {},
            })
            conn.execute(legacy.tables["chart_interpretation_data"].insert(), {"chart_id": 1, "raw_text": "Cached chart"})
            conn.execute(legacy.tables["email_verification_codes"].insert(), {
                "email": "pending@example.com", "code": "123456", "used": False,
                "expires_at": datetime.utcnow() + timedelta(minutes=10),
            })
        self.before = self.snapshot()
        self.script = (Path(__file__).parents[1] / "migrations" / "plan_usage.sql").read_text(encoding="utf-8")
        self.apply_script(self.script)
        self.paddle_script = (Path(__file__).parents[1] / "migrations" / "paddle_billing.sql").read_text(encoding="utf-8")
        self.apply_script(self.paddle_script)
        self.ai_script = (Path(__file__).parents[1] / "migrations" / "ai_conversation.sql").read_text(encoding="utf-8")
        self.apply_script(self.ai_script)
        self.birth_script = (Path(__file__).parents[1] / "migrations" / "birth_time_houses.sql").read_text(encoding="utf-8")
        self.apply_script(self.birth_script)
        self.apply_script((Path(__file__).parents[1] / 'migrations/relationships.sql').read_text(encoding='utf-8'))

    def test_birth_migration_preserves_legacy_data_without_inventing_timezone(self):
        self.assertEqual(self.snapshot(), self.before)
        with self.engine.connect() as conn:
            self.assertTrue(all(row == (None,None) for row in conn.exec_driver_sql('SELECT timezone,birth_utc FROM natal_charts').all()))
            self.assertTrue(all(row == (None,None) for row in conn.exec_driver_sql('SELECT houses,house_system FROM chart_data').all()))
        with self.assertRaises(Exception): self.apply_script(self.birth_script)
        self.assertEqual(self.snapshot(),self.before)

    def test_ai_migration_preserves_rows_and_refuses_reapplication(self):
        self.assertEqual(self.snapshot(), self.before)
        with self.engine.connect() as conn:
            self.assertIn('gpt_conversations', inspect(conn).get_table_names())
            columns = {c['name'] for c in inspect(conn).get_columns('gpt_usage')}
            self.assertTrue({'input_tokens','output_tokens','total_tokens','model','summary_usage'} <= columns)
        with self.assertRaises(Exception):
            self.apply_script(self.ai_script)
        self.assertEqual(self.snapshot(), self.before)

    def test_same_chart_cannot_answer_concurrently_with_spare_quota(self):
        barrier = Barrier(2)
        def attempt():
            with self.sessions() as db:
                barrier.wait(timeout=5)
                try:
                    return usage.reserve(db, 1, 1)
                except HTTPException as exc:
                    self.assertEqual(exc.detail['code'], 'GPT_REQUEST_IN_PROGRESS')
                    return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: attempt(), range(2)))
        winner, = [r for r in results if r is not None]
        with self.sessions() as db:
            usage.finalize(db, 1, winner, 'Question', 'Answer')
            self.assertEqual(db.query(GPTUsage).filter_by(status='succeeded',user_id=1).count(), 2)

    def test_paddle_migration_is_additive_and_cannot_reset_existing_data(self):
        self.assertEqual(self.snapshot(), self.before)
        with self.sessions() as db:
            self.assertTrue(all(user.payment_provider is None for user in db.query(User)))
        with self.assertRaises(Exception):
            self.apply_script(self.paddle_script)
        self.assertEqual(self.snapshot(), self.before)

    def snapshot(self):
        with self.engine.connect() as conn:
            return {table.name: [dict(row) for row in conn.execute(table.select().order_by(*table.primary_key)).mappings()]
                    for table in self.legacy.sorted_tables}

    def apply_script(self, script):
        raw = self.engine.raw_connection()
        try:
            # Keep the driver's normal transaction mode when returning to the
            # pool: autocommit would invalidate subsequent FOR UPDATE tests.
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
        if hasattr(self, "engine"):
            self.engine.dispose()
        # schema is generated above, never taken from a URL or user-provided SQL.
        with self.admin.begin() as conn:
            conn.exec_driver_sql(f'DROP SCHEMA "{self.schema}" CASCADE')
        self.admin.dispose()

    def test_exact_additive_migration_defaults_and_backfill_preserve_history(self):
        self.assertEqual(self.snapshot(), self.before)
        with self.sessions() as db:
            for user in db.query(User).all():
                self.assertEqual(user.plan, "free")
                self.assertIsNone(user.current_period_start)
                self.assertIsNone(user.current_period_end)
            self.assertEqual(db.query(GPTMessage).count(), 6)
            self.assertEqual(db.query(GPTUsage).filter_by(status="succeeded").count(), 2)
            self.assertEqual({row.source_message_id for row in db.query(GPTUsage)},
                             {row.id for row in db.query(GPTMessage).filter(GPTMessage.role == "user", GPTMessage.chart_id.in_([1, 2]))})
            state = usage.account_usage(db, 1)
            self.assertEqual(state["gpt_messages_used"], 1)
            self.assertEqual(state["saved_charts_limit"], 3)

    def test_repeat_application_fails_without_changing_data_or_usage(self):
        with self.sessions() as db:
            ledger_before = [(row.id, row.user_id, row.source_message_id, row.status) for row in db.query(GPTUsage).order_by(GPTUsage.id)]
        with self.assertRaises(Exception) as error:
            self.apply_script(self.script)
        self.assertEqual(error.exception.pgcode, "42701")  # duplicate column
        self.assertEqual(self.snapshot(), self.before)
        with self.sessions() as db:
            self.assertEqual([(row.id, row.user_id, row.source_message_id, row.status) for row in db.query(GPTUsage).order_by(GPTUsage.id)], ledger_before)
            self.assertEqual(usage.account_usage(db, 1)["gpt_messages_used"], 1)

    def test_failure_after_backfill_rolls_back_all_ddl_and_preserves_legacy_data(self):
        # Revert only this test's generated schema to its pre-feature shape.
        with self.engine.begin() as conn:
            conn.exec_driver_sql("DROP TABLE gpt_conversations")
            conn.exec_driver_sql("DROP INDEX ix_gpt_messages_chart_id_id")
            conn.exec_driver_sql("DROP TABLE gpt_usage")
            conn.exec_driver_sql("ALTER TABLE users DROP COLUMN plan, DROP COLUMN current_period_start, DROP COLUMN current_period_end")
        broken_script = self.script.replace("COMMIT;", "SELECT 1 / 0;\nCOMMIT;")
        with self.assertRaises(Exception) as error:
            self.apply_script(broken_script)
        self.assertEqual(error.exception.pgcode, "22012")  # division by zero, after DDL + backfill
        self.assertEqual(self.snapshot(), self.before)
        with self.engine.connect() as conn:
            self.assertNotIn("gpt_usage", inspect(conn).get_table_names())
            self.assertNotIn("plan", {column["name"] for column in inspect(conn).get_columns("users")})
        self.apply_script(self.script)
        self.apply_script(self.ai_script)
        self.assertEqual(self.snapshot(), self.before)
        # This deliberately reconstructed historical schema predates subject-aware ORM fields.
        with self.engine.connect() as conn:
            self.assertEqual(conn.execute(text(
                "SELECT count(*) FROM gpt_usage WHERE user_id = 1 AND status = 'succeeded'"
            )).scalar_one(), 1)

    def test_for_update_really_blocks_then_releases_the_waiting_reservation(self):
        ready = Event()
        worker = {}
        def attempt():
            with self.sessions() as db:
                worker["pid"] = db.execute(text("SELECT pg_backend_pid()")).scalar_one()
                ready.set()
                result = usage.reserve(db, 1, 1)
                self.assertFalse(db.in_transaction())
                return result
        with self.sessions() as holder, ThreadPoolExecutor(max_workers=1) as pool:
            usage.locked_user(holder, 1)
            holder_pid = holder.execute(text("SELECT pg_backend_pid()")).scalar_one()
            future = pool.submit(attempt)
            try:
                self.assertTrue(ready.wait(5))
                deadline = monotonic() + 5
                blockers = []
                while monotonic() < deadline:
                    with self.engine.connect() as observer:
                        blockers = observer.execute(text("SELECT pg_blocking_pids(:pid)"), {"pid": worker["pid"]}).scalar_one()
                    if holder_pid in blockers:
                        break
                    sleep(0.05)
                self.assertIn(holder_pid, blockers)
                self.assertFalse(future.done())
            finally:
                holder.rollback()
            reservation = future.result(timeout=5)
        with self.admin.connect() as observer:
            self.assertIsNone(observer.execute(text("SELECT xact_start FROM pg_stat_activity WHERE pid = :pid"), {"pid": worker["pid"]}).scalar_one())
        with self.sessions() as db:
            usage.release(db, 1, reservation)
            self.assertEqual(usage.account_usage(db, 1)["gpt_messages_reserved"], 0)

    def test_two_connections_cannot_both_reserve_last_free_or_premium_slot(self):
        for plan, seed_count in [("free", 8), ("premium", 299)]:
            with self.subTest(plan=plan):
                now = datetime.utcnow()
                start, end = now - timedelta(days=1), now + timedelta(days=29)
                with self.sessions() as db:
                    if plan == "premium":
                        usage.set_plan(db, 1, plan, start, end)
                    db.add_all([GPTUsage(user_id=1, chart_id=1, status="succeeded", plan=plan,
                                        period_start=start if plan == "premium" else None,
                                        period_end=end if plan == "premium" else None) for _ in range(seed_count)])
                    db.commit()
                barrier = Barrier(2)
                def attempt():
                    with self.sessions() as db:
                        barrier.wait(timeout=5)
                        try:
                            result = usage.reserve(db, 1, 1)
                            self.assertFalse(db.in_transaction())
                            return result
                        except HTTPException as error:
                            self.assertEqual(error.status_code, 409)
                            self.assertFalse(db.in_transaction())
                            return None
                with ThreadPoolExecutor(max_workers=2) as pool:
                    results = list(pool.map(lambda _: attempt(), range(2)))
                winners = [result for result in results if result is not None]
                self.assertEqual(len(winners), 1)
                with self.sessions() as db:
                    usage.finalize(db, 1, winners[0], "Question", "Mock answer")
                    state = usage.account_usage(db, 1)
                    self.assertEqual(state["gpt_messages_used"], state["gpt_messages_limit"])
