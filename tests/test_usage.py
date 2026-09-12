"""Plan/accounting API tests on isolated SQLite, with no real OpenAI calls."""
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch
from sqlalchemy import event
import test_my_charts as fixtures
from test_my_charts import PAYLOAD, app, get_db, get_password_hash
from database.queries import User, NatalChart, GPTMessage, GPTUsage, ChartInterpretationData, ChartData
from modules import usage
from modules.chart_limits import saved_chart_limit
from modules.plans import PLAN_LIMITS


class UsageTests(unittest.TestCase):
    setUp = fixtures.MyChartsTests.setUp
    tearDown = fixtures.MyChartsTests.tearDown
    chart = fixtures.MyChartsTests.chart
    mock_calculations = fixtures.MyChartsTests.mock_calculations
    verification_code = fixtures.MyChartsTests.verification_code

    def premium(self, start=None, end=None):
        now = datetime.utcnow()
        with self.sessions() as db:
            usage.set_plan(db, 1, "premium", start or now - timedelta(days=1), end or now + timedelta(days=29))

    def state(self):
        response = self.client.get("/account/usage", headers=self.owner)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def ask(self, chart_id):
        return self.client.post("/ask-gpt", headers=self.owner, json={"chart_id": chart_id, "question": "Question"})

    def seed_usage(self, count, plan="free", period_start=None, period_end=None):
        with self.sessions() as db:
            db.add_all([GPTUsage(user_id=1, chart_id=1000, status="succeeded", plan=plan,
                                period_start=period_start, period_end=period_end) for _ in range(count)])
            db.commit()

    def test_new_users_are_free_and_client_cannot_select_plan(self):
        self.verification_code()
        response = self.client.post("/auth/verify-code?code=123456", json={
            "email": "new@example.com", "password": "test-password-only", "plan": "premium"})
        self.assertEqual(response.status_code, 200, response.text)
        with self.sessions() as db:
            user = db.query(User).filter_by(email="new@example.com").one()
            self.assertEqual(user.plan, "free")
            self.assertEqual(saved_chart_limit(user), PLAN_LIMITS["free"].max_saved_charts)
        state = self.state()
        self.assertEqual((state["plan"], state["saved_charts_limit"], state["gpt_messages_limit"]), ("free", 3, 10))
        self.assertEqual(state["gpt_limit_type"], "lifetime")
        self.assertIsNone(state["current_period_start"])
        for headers in ({}, self.guest, {"Authorization": "Bearer invalid", **self.guest}):
            self.assertEqual(self.client.get("/account/usage", headers=headers).status_code, 401)

    def test_premium_chart_limit_and_downgrade_keep_existing_charts(self):
        self.premium()
        with self.mock_calculations():
            for _ in range(10):
                response = self.client.post("/natal-chart", json=PAYLOAD, headers=self.owner)
                self.assertEqual(response.status_code, 200, response.text)
            denied = self.client.post("/natal-chart", json=PAYLOAD, headers=self.owner)
            self.assertEqual(denied.status_code, 409)
            self.assertEqual(denied.json()["detail"]["limit"], 10)
            self.assertEqual(denied.json()["detail"]["plan"], "premium")
        with self.sessions() as db:
            usage.set_plan(db, 1, "free")
        self.assertEqual(self.state()["saved_charts_used"], 10)
        denied = self.client.post("/natal-chart", json=PAYLOAD, headers=self.owner)
        self.assertEqual(denied.status_code, 409)
        self.assertEqual(denied.json()["detail"]["used"], 10)
        self.assertEqual(denied.json()["detail"]["limit"], 3)
        self.assertEqual(self.client.get("/natal-charts", headers=self.owner).json()["count"], 10)

    def test_selected_login_migration_uses_premium_limit_and_auth_still_succeeds(self):
        self.premium()
        with self.sessions() as db:
            user = db.get(User, 1)
            user.email = "existing@example.com"
            user.password_hash = get_password_hash("test-password-only")
            db.commit()
        for _ in range(9):
            self.chart(related=False)
        guests = [self.chart(user_id=None, session_token=self.guest_token) for _ in range(2)]
        for chart_id, expected in zip(guests, ["migrated", "limit_reached"]):
            response = self.client.post("/auth/login", headers=self.guest, json={
                "email": "existing@example.com", "password": "test-password-only", "guest_chart_id": chart_id})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["guest_chart_migration"], {"status": expected, "chart_id": chart_id})
        with self.sessions() as db:
            self.assertEqual(db.get(NatalChart, guests[0]).user_id, 1)
            self.assertIsNone(db.get(NatalChart, guests[1]).user_id)

    def test_free_ten_successes_across_charts_and_delete_does_not_reset_usage(self):
        charts = [self.chart(related=False) for _ in range(3)]
        with patch("api.endpoints.ChartInterpreter") as interpreter:
            interpreter.return_value.ask_gpt = AsyncMock(return_value="Answer")
            for number in range(10):
                response = self.ask(charts[number % len(charts)])
                self.assertEqual(response.status_code, 200, response.text)
            response = self.ask(charts[0])
            self.assertEqual(response.status_code, 409)
            self.assertEqual(response.json()["detail"]["code"], "GPT_LIMIT_REACHED")
            self.assertEqual(interpreter.call_count, 10)
        self.assertEqual(self.state()["gpt_messages_used"], 10)
        self.assertEqual(self.client.delete(f"/natal-chart/{charts[0]}", headers=self.owner).status_code, 204)
        self.assertEqual(self.state()["gpt_messages_used"], 10)
        with patch("api.endpoints.ChartInterpreter") as interpreter:
            self.assertEqual(self.ask(charts[1]).status_code, 409)
            interpreter.assert_not_called()

    def test_legacy_user_history_is_imported_once_and_survives_deletion(self):
        chart_id = self.chart()  # one user and one assistant
        self.chart(user_id=2)
        guest_id = self.chart(user_id=None, session_token=self.guest_token)
        self.assertEqual(self.state()["gpt_messages_used"], 1)
        self.assertEqual(self.state()["gpt_messages_used"], 1)
        with self.sessions() as db:
            self.assertEqual(db.query(GPTMessage).count(), 6)
            self.assertEqual(db.query(GPTUsage).count(), 1)
        self.assertEqual(self.client.delete(f"/natal-chart/{chart_id}", headers=self.owner).status_code, 204)
        self.assertEqual(self.state()["gpt_messages_used"], 1)
        # Legacy guest history becomes attributed only after the existing transfer.
        with self.sessions() as db:
            from modules.migration import migrate_guest_data_to_user
            migrate_guest_data_to_user(db, 1, self.guest_token, guest_id)
            db.commit()
        self.assertEqual(self.state()["gpt_messages_used"], 2)

    def test_premium_period_limit_extension_renewal_and_free_lifetime(self):
        start = datetime.utcnow() - timedelta(days=1)
        end = start + timedelta(days=30)
        self.premium(start, end)
        self.seed_usage(299, "premium", start, end)
        self.seed_usage(4, "premium", start - timedelta(days=30), start)
        chart_id = self.chart(related=False)
        with patch("api.endpoints.ChartInterpreter") as interpreter:
            interpreter.return_value.ask_gpt = AsyncMock(return_value="Answer")
            self.assertEqual(self.ask(chart_id).status_code, 200)
            self.assertEqual(self.ask(chart_id).status_code, 409)
            self.assertEqual(interpreter.call_count, 1)
        state = self.state()
        self.assertEqual((state["plan"], state["gpt_messages_used"], state["gpt_messages_limit"]), ("premium", 300, 300))
        self.assertEqual(state["gpt_limit_type"], "billing_period")
        self.assertEqual(state["saved_charts_limit"], 10)
        self.premium(start, end + timedelta(days=1))
        self.assertEqual(self.state()["gpt_messages_used"], 300)
        new_start = end + timedelta(days=1)
        self.premium(new_start, new_start + timedelta(days=30))
        with patch("modules.usage.utcnow", return_value=new_start + timedelta(seconds=1)):
            self.assertEqual(self.state()["gpt_messages_used"], 0)
            with patch("api.endpoints.ChartInterpreter") as interpreter:
                interpreter.return_value.ask_gpt = AsyncMock(return_value="New period answer")
                self.assertEqual(self.ask(chart_id).status_code, 200)
        with self.sessions() as db:
            usage.set_plan(db, 1, "free")
        self.assertEqual(self.state()["gpt_messages_used"], 305)

    def test_premium_missing_future_expired_or_inverted_period_fails_closed(self):
        chart_id = self.chart(related=False)
        now = datetime.utcnow()
        for start, end in [(None, None), (None, now), (now, None),
                           (now + timedelta(days=1), now + timedelta(days=2)),
                           (now - timedelta(days=2), now - timedelta(days=1)), (now, now)]:
            with self.subTest(start=start, end=end):
                with self.sessions() as db:
                    user = db.get(User, 1)
                    user.plan, user.current_period_start, user.current_period_end = "premium", start, end
                    db.commit()
                with patch("api.endpoints.ChartInterpreter") as interpreter:
                    response = self.ask(chart_id)
                    self.assertEqual(response.status_code, 409)
                    self.assertEqual(response.json()["detail"]["code"], "GPT_PERIOD_INVALID")
                    interpreter.assert_not_called()
                self.assertEqual(self.state()["gpt_messages_available"], 0)

    def test_openai_failures_and_internal_persistence_failure_release_quota(self):
        chart_id = self.chart(related=False)
        for failure in [RuntimeError("provider error"), TimeoutError("timeout")]:
            with patch("api.endpoints.ChartInterpreter") as interpreter:
                interpreter.return_value.ask_gpt = AsyncMock(side_effect=failure)
                self.assertEqual(self.ask(chart_id).status_code, 500)
            self.assertEqual(self.state()["gpt_messages_used"], 0)
            self.assertEqual(self.state()["gpt_messages_reserved"], 0)
        def fail_insert(conn, cursor, statement, *args):
            if statement.startswith("INSERT INTO gpt_messages"):
                raise RuntimeError("persistence failed")
        event.listen(self.engine, "before_cursor_execute", fail_insert)
        try:
            with patch("api.endpoints.ChartInterpreter") as interpreter:
                interpreter.return_value.ask_gpt = AsyncMock(return_value="Answer")
                self.assertEqual(self.ask(chart_id).status_code, 500)
        finally:
            event.remove(self.engine, "before_cursor_execute", fail_insert)
        self.assertEqual(self.state()["gpt_messages_used"], 0)
        self.assertEqual(self.state()["gpt_messages_reserved"], 0)
        with self.sessions() as db:
            self.assertEqual(db.query(GPTMessage).count(), 0)

    def test_last_slot_is_reserved_while_openai_waits_without_open_transaction(self):
        chart_id = self.chart(related=False)
        self.seed_usage(9)
        started, finish = threading.Event(), threading.Event()
        sessions = []
        def tracked_db():
            with self.sessions() as db:
                sessions.append(db)
                yield db
        app.dependency_overrides[get_db] = tracked_db
        async def slow_answer(*args):
            self.assertFalse(any(db.in_transaction() for db in sessions))
            started.set()
            self.assertTrue(finish.wait(timeout=5))
            return "Answer"
        with patch("api.endpoints.ChartInterpreter") as interpreter, ThreadPoolExecutor(max_workers=1) as pool:
            interpreter.return_value.ask_gpt = AsyncMock(side_effect=slow_answer)
            future = pool.submit(self.ask, chart_id)
            try:
                self.assertTrue(started.wait(timeout=5))
                second = self.ask(chart_id)
                self.assertEqual(second.status_code, 409, second.text)
                self.assertEqual(second.json()["detail"]["reserved"], 1)
            finally:
                finish.set()
            self.assertEqual(future.result(timeout=5).status_code, 200)
            self.assertEqual(interpreter.call_count, 1)
        self.assertEqual(self.state()["gpt_messages_used"], 10)

    def test_expired_reservation_cannot_finalize_or_consume_quota_twice(self):
        chart_id = self.chart(related=False)
        self.seed_usage(9)
        now = datetime.utcnow()
        with self.sessions() as db, patch("modules.usage.utcnow", return_value=now):
            first = usage.reserve(db, 1, chart_id)
            self.assertFalse(db.in_transaction())
        with self.sessions() as db, patch("modules.usage.utcnow", return_value=now + usage.RESERVATION_TTL):
            second = usage.reserve(db, 1, chart_id)
            with self.assertRaises(Exception) as error:
                usage.finalize(db, 1, first, "Question", "Late answer")
            self.assertEqual(error.exception.status_code, 409)
            usage.finalize(db, 1, second, "Question", "Answer")
            usage.release(db, 1, second)  # must never refund an already committed success
        self.assertEqual(self.state()["gpt_messages_used"], 10)
        with self.sessions() as db:
            self.assertEqual(db.query(GPTMessage).count(), 2)

    def test_real_interpreter_closes_cache_hit_and_miss_transactions_before_network(self):
        from types import SimpleNamespace
        sessions = []
        def tracked_db():
            with self.sessions() as db:
                sessions.append(db)
                yield db
        app.dependency_overrides[get_db] = tracked_db
        def mock_openai(**kwargs):
            self.assertFalse(any(db.in_transaction() for db in sessions))
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="Answer"))])
        for cached in (False, True):
            with self.subTest(cached=cached):
                chart_id = self.chart(related=False)
                with self.sessions() as db:
                    db.add(ChartData(chart_id=chart_id, bodies_for_circle=fixtures.BODY, aspects_for_circle=[]))
                    db.commit()
                if cached:
                    with self.sessions() as db:
                        db.add(ChartInterpretationData(chart_id=chart_id, raw_text="Cached"))
                        db.commit()
                with patch("modules.interpretation.client.chat.completions.create", side_effect=mock_openai) as openai:
                    response = self.ask(chart_id)
                    self.assertEqual(response.status_code, 200, response.text)
                    openai.assert_called_once()

    def test_delete_during_gpt_releases_reservation_without_saved_usage(self):
        chart_id = self.chart(related=False)
        with self.sessions() as db:
            reservation = usage.reserve(db, 1, chart_id)
        self.assertEqual(self.client.delete(f"/natal-chart/{chart_id}", headers=self.owner).status_code, 204)
        with self.sessions() as db:
            with self.assertRaises(Exception) as error:
                usage.finalize(db, 1, reservation, "Question", "Answer")
            self.assertEqual(error.exception.status_code, 404)
            usage.release(db, 1, reservation)
        self.assertEqual(self.state()["gpt_messages_used"], 0)
        self.assertEqual(self.state()["gpt_messages_reserved"], 0)

    def test_plan_transition_during_request_keeps_original_period_and_rejects_overlap(self):
        now = datetime.utcnow()
        start, end = now - timedelta(days=1), now + timedelta(days=1)
        self.premium(start, end)
        chart_id = self.chart(related=False)
        with self.sessions() as db:
            reservation = usage.reserve(db, 1, chart_id)
            with self.assertRaises(ValueError):
                usage.set_plan(db, 1, "premium", start + timedelta(hours=1), end + timedelta(days=1))
            self.assertFalse(db.in_transaction())
            usage.set_plan(db, 1, "premium", end, end + timedelta(days=30))
            usage.finalize(db, 1, reservation, "Question", "Answer")
        with patch("modules.usage.utcnow", return_value=end + timedelta(seconds=1)):
            self.assertEqual(self.state()["gpt_messages_used"], 0)
        with self.sessions() as db:
            usage.set_plan(db, 1, "free")
        self.assertEqual(self.state()["gpt_messages_used"], 1)

    def test_deleting_all_history_keeps_usage_and_does_not_block_future_success(self):
        chart_id = self.chart()
        self.assertEqual(self.client.delete(f"/natal-chart/{chart_id}", headers=self.owner).status_code, 204)
        self.assertEqual(self.state()["gpt_messages_used"], 1)
        with self.sessions() as db:
            self.assertIsNone(db.query(GPTUsage).one().source_message_id)
        new_chart = self.chart(related=False)
        with patch("api.endpoints.ChartInterpreter") as interpreter:
            interpreter.return_value.ask_gpt = AsyncMock(return_value="Answer")
            response = self.ask(new_chart)
            self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.state()["gpt_messages_used"], 2)
