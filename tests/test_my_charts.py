"""Isolated API regression tests. No .env, application DB, email or GPT calls.

Run: .venv/Scripts/python -m unittest discover -s tests -v
SQLite validates API behaviour and foreign keys, not PostgreSQL row locking.
"""
import os
import asyncio
import threading
import unittest
from contextlib import ExitStack
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from fastapi.testclient import TestClient
from httpx import AsyncClient, ASGITransport

with patch("dotenv.load_dotenv"), patch.dict(os.environ, {
    "DATABASE_URL": "sqlite://",
    "SECRET_KEY": "my-charts-isolated-tests-only",
    "OPENAI_API_KEY": "test-not-a-real-key",
}):
    from main import app
    from api.auth import create_access_token, get_current_user_or_guest, get_password_hash
    from api.endpoints import _create_natal_chart
    from database.connection import Base, get_db
    from database.queries import User, NatalChart, ChartData, ChartInterpretationData, GPTMessage, EmailVerificationCode
    from modules.ephemeris import Ephemeris
    from modules.migration import migrate_guest_data_to_user


BODY = {"☉": {"symbol": "☉", "label": "Солнце", "degree": 10.0,
                "roundedDegree": "10°", "sign": "Овен", "house": 1, "retrograde": False}}
PAYLOAD = {"year": 2000, "month": 1, "day": 2, "hour": 12.0,
           "lon": 30.0, "lat": 50.0, "city": "Test city", "region": "", "country": ""}


class MyChartsTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", poolclass=StaticPool,
                                    connect_args={"check_same_thread": False})
        event.listen(self.engine, "connect", lambda connection, _: connection.execute("PRAGMA foreign_keys=ON"))
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine, autoflush=False)

        def test_db():
            with self.sessions() as session:
                yield session

        app.dependency_overrides[get_db] = test_db
        self.client = TestClient(app, raise_server_exceptions=False)
        with self.sessions() as session:
            session.add_all([User(id=1, email="a@example.test", password_hash="unused"),
                             User(id=2, email="b@example.test", password_hash="unused")])
            session.commit()
        self.owner = {"Authorization": "Bearer " + create_access_token({"sub": "1"})}
        self.other = {"Authorization": "Bearer " + create_access_token({"sub": "2"})}
        self.guest_token = uuid4()
        self.guest = {"X-Session-Token": str(self.guest_token)}

    def tearDown(self):
        self.client.close()
        app.dependency_overrides.clear()
        self.engine.dispose()

    def chart(self, user_id=1, session_token=None, related=True):
        with self.sessions() as session:
            chart = NatalChart(**PAYLOAD, user_id=user_id, session_token=session_token)
            session.add(chart)
            session.flush()
            # Explicitly clear the model's UUID default when testing ownerless records.
            chart.session_token = session_token
            chart_id = chart.id
            if related:
                session.add_all([
                    ChartData(chart_id=chart_id, bodies_for_circle=BODY, aspects_for_circle=[],
                              points_data=[], patterns_data=[], aspects_structured={}),
                    ChartInterpretationData(chart_id=chart_id, raw_text="cached chart"),
                    GPTMessage(chart_id=chart_id, role="user", content="Existing question"),
                    GPTMessage(chart_id=chart_id, role="gpt", content="Existing answer"),
                ])
            session.commit()
            return chart_id

    def mock_calculations(self):
        stack = ExitStack()
        ephem = stack.enter_context(patch("api.endpoints.Ephemeris")).return_value
        ephem.get_all_bodies_with_degrees.return_value = BODY
        ephem.get_all_bodies_with_symbols.return_value = {}
        ephem.get_all_bodies.return_value = {}
        ephem.get_house_cusps.return_value = [{"symbol": str(i), "degree": i * 30.0} for i in range(1, 13)]
        aspects = stack.enter_context(patch("api.endpoints.Aspects")).return_value
        aspects.get_all_aspects_flat.return_value = ""
        aspects.convert_aspects_for_chart.return_value = []
        aspects.convert_aspects_to_symbols.return_value = {}
        aspects.get_all_aspects_structured.return_value = {}
        patterns = stack.enter_context(patch("api.endpoints.AstrologicalPatterns")).return_value
        patterns.get_patterns_structured.return_value = []
        return stack

    def test_list_is_authenticated_and_owner_scoped(self):
        own_id = self.chart()
        self.chart(user_id=2)
        self.chart(user_id=None, session_token=self.guest_token)
        for headers in ({}, self.guest, {"Authorization": "Bearer invalid"}):
            self.assertEqual(self.client.get("/natal-charts", headers=headers).status_code, 401)
        response = self.client.get("/natal-charts", headers=self.owner)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"charts": [{"chart_id": own_id, "year": 2000,
                         "month": 1, "day": 2, "hour": 12.0, "city": "Test city"}], "count": 1, "limit": 3})

    def test_get_ownership_and_reconstructed_houses(self):
        own_id = self.chart(session_token=self.guest_token)
        guest_id = self.chart(user_id=None, session_token=self.guest_token)
        orphan_id = self.chart(user_id=None)
        for chart_id, headers in [(own_id, self.other), (own_id, self.guest),
                                  (guest_id, {"X-Session-Token": str(uuid4())}),
                                  (guest_id, self.other), (orphan_id, self.owner),
                                  (orphan_id, self.guest), (99999, self.owner)]:
            with self.subTest(chart_id=chart_id, headers=list(headers)):
                self.assertEqual(self.client.get(f"/natal-chart/{chart_id}", headers=headers).status_code, 404)
        expected_houses = Ephemeris(2000, 1, 2, 12.0, 30.0, 50.0).get_house_cusps()
        for chart_id, headers in [(own_id, self.owner), (guest_id, self.guest)]:
            response = self.client.get(f"/natal-chart/{chart_id}", headers=headers)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["houses"], expected_houses)
            self.assertEqual(len(response.json()["houses"]), 12)
            self.assertEqual(response.json()["bodies_for_circle"], BODY)

    def test_delete_requires_owner_and_removes_all_dependencies(self):
        chart_id = self.chart()
        other_id = self.chart(user_id=2)
        for headers, status in [({}, 401), (self.guest, 401), (self.other, 404)]:
            self.assertEqual(self.client.delete(f"/natal-chart/{chart_id}", headers=headers).status_code, status)
        response = self.client.delete(f"/natal-chart/{chart_id}", headers=self.owner)
        self.assertEqual(response.status_code, 204, response.text)
        self.assertEqual(response.content, b"")
        with self.sessions() as session:
            self.assertIsNone(session.get(NatalChart, chart_id))
            self.assertIsNotNone(session.get(NatalChart, other_id))
            for model in (ChartData, ChartInterpretationData, GPTMessage):
                self.assertEqual(session.query(model).filter_by(chart_id=chart_id).count(), 0)
                self.assertGreater(session.query(model).filter_by(chart_id=other_id).count(), 0)
        self.assertEqual(self.client.delete(f"/natal-chart/{chart_id}", headers=self.owner).status_code, 404)

    def test_delete_cannot_claim_guest_chart(self):
        chart_id = self.chart(user_id=None, session_token=self.guest_token)
        self.assertEqual(self.client.delete(f"/natal-chart/{chart_id}", headers={**self.owner, **self.guest}).status_code, 404)

    def test_delete_failure_rolls_back_children(self):
        chart_id = self.chart()

        def fail_delete(conn, cursor, statement, parameters, context, executemany):
            if statement.startswith("DELETE FROM chart_data"):
                raise RuntimeError("simulated deletion failure")

        event.listen(self.engine, "before_cursor_execute", fail_delete)
        try:
            self.assertEqual(self.client.delete(f"/natal-chart/{chart_id}", headers=self.owner).status_code, 500)
        finally:
            event.remove(self.engine, "before_cursor_execute", fail_delete)
        with self.sessions() as session:
            self.assertIsNotNone(session.get(NatalChart, chart_id))
            self.assertEqual(session.query(GPTMessage).filter_by(chart_id=chart_id).count(), 2)
            self.assertEqual(session.query(ChartInterpretationData).filter_by(chart_id=chart_id).count(), 1)

    def test_limit_and_slot_released_after_delete(self):
        first = self.chart()
        self.chart()
        with self.mock_calculations():
            self.assertEqual(self.client.post("/natal-chart", json=PAYLOAD, headers=self.owner).status_code, 200)
            response = self.client.post("/natal-chart", json=PAYLOAD, headers=self.owner)
            self.assertEqual(response.status_code, 409)
            self.assertEqual(response.json()["detail"]["code"], "CHART_LIMIT_REACHED")
            self.assertEqual(self.client.post("/natal-chart", json=PAYLOAD, headers=self.other).status_code, 200)
            self.assertEqual(self.client.delete(f"/natal-chart/{first}", headers=self.owner).status_code, 204)
            self.assertEqual(self.client.post("/natal-chart", json=PAYLOAD, headers=self.owner).status_code, 200)
        self.assertEqual(self.client.get("/natal-charts", headers=self.owner).json()["count"], 3)

    def test_guest_creation_keeps_existing_behaviour(self):
        with self.mock_calculations():
            for _ in range(4):
                response = self.client.post("/natal-chart", json=PAYLOAD, headers=self.guest)
                self.assertEqual(response.status_code, 200, response.text)
        with self.sessions() as session:
            self.assertEqual(session.query(NatalChart).filter_by(user_id=None, session_token=self.guest_token).count(), 4)

    def test_create_runs_outside_event_loop(self):
        def check_thread(*args):
            with self.assertRaises(RuntimeError):
                asyncio.get_running_loop()
            return _create_natal_chart(*args)

        with self.mock_calculations(), patch("api.endpoints._create_natal_chart", side_effect=check_thread):
            response = self.client.post("/natal-chart", json=PAYLOAD, headers=self.owner)
        self.assertEqual(response.status_code, 200, response.text)

    def test_409_rolls_back_before_dependency_cleanup(self):
        for _ in range(3):
            self.chart()
        observations = []

        def tracked_db():
            db = self.sessions()
            try:
                with patch.object(db, "rollback", wraps=db.rollback) as rollback:
                    try:
                        yield db
                    finally:
                        observations.append((rollback.call_count, db.in_transaction()))
            finally:
                db.close()
                observations.append(db.in_transaction())

        app.dependency_overrides[get_db] = tracked_db
        with patch("api.endpoints.Ephemeris") as calculate:
            response = self.client.post("/natal-chart", json=PAYLOAD, headers=self.owner)
            calculate.assert_not_called()
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(observations, [(1, False), False])
        # The next request must still be able to use a fresh session.
        self.assertEqual(self.client.get("/natal-charts", headers=self.owner).json()["count"], 3)

    def test_success_finishes_transaction_before_dependency_cleanup(self):
        observations = []

        def tracked_db():
            with self.sessions() as db:
                try:
                    yield db
                finally:
                    observations.append(db.in_transaction())

        app.dependency_overrides[get_db] = tracked_db
        with self.mock_calculations():
            response = self.client.post("/natal-chart", json=PAYLOAD, headers=self.owner)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(observations, [False])

    def test_calculation_error_rolls_back_before_dependency_cleanup(self):
        observations = []

        def tracked_db():
            with self.sessions() as db:
                try:
                    yield db
                finally:
                    observations.append(db.in_transaction())

        app.dependency_overrides[get_db] = tracked_db
        with patch("api.endpoints.Ephemeris", side_effect=RuntimeError("simulated calculation failure")):
            response = self.client.post("/natal-chart", json=PAYLOAD, headers=self.owner)
        self.assertEqual(response.status_code, 500)
        self.assertEqual(observations, [False])

    def test_concurrent_409_releases_simulated_row_lock(self):
        # Real ASGI scheduling with a blocking lock; this is not a PostgreSQL test.
        lock = threading.Lock()
        sessions = []

        class LockedSession:
            held = False
            rolled_back = False
            closed = False

            def query(self, model): return self
            def filter(self, *args): return self
            def populate_existing(self): return self
            def with_for_update(self): return self

            def one(self):
                if not lock.acquire(timeout=1):
                    raise RuntimeError("row lock timeout")
                self.held = True
                return SimpleNamespace(id=1, plan="free")

            def count(self): return 3

            def rollback(self):
                self.rolled_back = True
                if self.held:
                    self.held = False
                    lock.release()

            def close(self):
                self.closed = True
                if self.held:
                    self.held = False
                    lock.release()

        def locked_db():
            db = LockedSession()
            sessions.append(db)
            try:
                yield db
            finally:
                db.close()

        async def identity():
            return {"user": SimpleNamespace(id=1), "session_token": None}

        async def concurrent_requests():
            async with AsyncClient(transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test") as client:
                return await asyncio.gather(*[client.post("/natal-chart", json=PAYLOAD) for _ in range(4)])

        app.dependency_overrides[get_db] = locked_db
        app.dependency_overrides[get_current_user_or_guest] = identity
        responses = asyncio.run(concurrent_requests())
        self.assertEqual([response.status_code for response in responses], [409] * 4)
        self.assertFalse(lock.locked())
        self.assertTrue(all(db.rolled_back and db.closed for db in sessions))

    def verification_code(self, email="new@example.com"):
        with self.sessions() as session:
            session.add(EmailVerificationCode(email=email, code="123456", used=False,
                        expires_at=datetime.utcnow() + timedelta(minutes=10)))
            session.commit()

    def legacy_history(self, chart_id):
        # Historical guest messages remain in storage but are no longer public
        # through guest auth. Read the fixture directly before migrating it.
        with self.sessions() as db:
            return [(msg.id, msg.role, msg.content) for msg in db.query(GPTMessage)
                    .filter_by(chart_id=chart_id).order_by(GPTMessage.created_at).all()]

    def test_verification_migrates_guest_chart_and_preserves_history(self):
        chart_id = self.chart(user_id=None, session_token=self.guest_token)
        other_guest_id = self.chart(user_id=None, session_token=uuid4())
        other_user_id = self.chart(user_id=2)
        history_before = self.legacy_history(chart_id)
        self.verification_code()
        response = self.client.post("/auth/verify-code?code=123456", headers=self.guest,
                                    json={"email": "new@example.com", "password": "test-password-only", "guest_chart_id": chart_id})
        self.assertEqual(response.status_code, 200, response.text)
        registered = {"Authorization": "Bearer " + response.json()["access_token"]}
        new_user_id = self.client.get("/auth/me", headers=registered).json()["id"]
        with self.sessions() as session:
            chart = session.get(NatalChart, chart_id)
            self.assertEqual(chart.user_id, new_user_id)
            self.assertIsNone(chart.session_token)
            self.assertEqual(session.query(NatalChart).count(), 3)
            self.assertIsNone(session.get(NatalChart, other_guest_id).user_id)
            self.assertEqual(session.get(NatalChart, other_user_id).user_id, 2)
            self.assertTrue(session.get(EmailVerificationCode, "new@example.com").used)
        opened = self.client.get(f"/natal-chart/{chart_id}", headers=registered)
        self.assertEqual(opened.status_code, 200, opened.text)
        self.assertEqual(opened.json()["chart_id"], chart_id)
        history = self.client.get(f"/gpt-messages?chart_id={chart_id}", headers=registered)
        self.assertEqual(history.status_code, 200)
        self.assertEqual([(msg["id"], msg["role"], msg["content"]) for msg in history.json()], history_before)
        self.assertEqual(self.client.get(f"/natal-chart/{chart_id}", headers=self.guest).status_code, 404)

    def test_verification_without_guest_header_still_works(self):
        self.verification_code()
        response = self.client.post("/auth/verify-code?code=123456",
                                    json={"email": "new@example.com", "password": "test-password-only"})
        self.assertEqual(response.status_code, 200, response.text)

    def test_invalid_guest_header_does_not_create_user_or_consume_code(self):
        self.verification_code()
        response = self.client.post("/auth/verify-code?code=123456", headers={"X-Session-Token": "invalid"},
                                    json={"email": "new@example.com", "password": "test-password-only"})
        self.assertEqual(response.status_code, 422)
        with self.sessions() as session:
            self.assertIsNone(session.query(User).filter_by(email="new@example.com").first())
            self.assertFalse(session.get(EmailVerificationCode, "new@example.com").used)

    def test_wrong_verification_code_keeps_guest_chart_accessible(self):
        chart_id = self.chart(user_id=None, session_token=self.guest_token)
        self.verification_code()
        response = self.client.post("/auth/verify-code?code=000000", headers=self.guest,
                                    json={"email": "new@example.com", "password": "test-password-only"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.client.get(f"/natal-chart/{chart_id}", headers=self.guest).status_code, 200)

    def test_login_still_migrates_guest_chart(self):
        with self.sessions() as session:
            user = session.get(User, 1)
            user.email = "existing@example.com"
            user.password_hash = get_password_hash("test-password-only")
            session.commit()
        chart_id = self.chart(user_id=None, session_token=self.guest_token)
        history_before = self.legacy_history(chart_id)
        response = self.client.post("/auth/login", headers=self.guest,
                                    json={"email": "existing@example.com", "password": "test-password-only", "guest_chart_id": chart_id})
        self.assertEqual(response.status_code, 200, response.text)
        headers = {"Authorization": "Bearer " + response.json()["access_token"]}
        self.assertEqual(self.client.get(f"/natal-chart/{chart_id}", headers=headers).status_code, 200)
        history = self.client.get(f"/gpt-messages?chart_id={chart_id}", headers=headers)
        self.assertEqual(history.status_code, 200)
        self.assertEqual([(msg["id"], msg["role"], msg["content"]) for msg in history.json()], history_before)
        with self.sessions() as session:
            self.assertEqual(session.get(NatalChart, chart_id).user_id, 1)

    def migration_auth(self, flow, chart_id=None, headers=None, saved_count=0, fail_token=False):
        email = f"migration-{uuid4().hex}@example.com"
        password = "test-password-only"
        if flow == "login":
            with self.sessions() as db:
                user = User(email=email, password_hash=get_password_hash(password))
                db.add(user)
                db.flush()
                for _ in range(saved_count):
                    db.add(NatalChart(**PAYLOAD, user_id=user.id))
                db.commit()
        else:
            self.verification_code(email)

        def transfer(db, user_id, session_token, guest_chart_id):
            # A newly verified account normally has zero charts. Seed this rare
            # branch explicitly to test verify's handling of the real limit result.
            if flow == "verify-code":
                for _ in range(saved_count):
                    db.add(NatalChart(**PAYLOAD, user_id=user_id))
                db.flush()
            return migrate_guest_data_to_user(db, user_id, session_token, guest_chart_id)

        observations = []
        def tracked_db():
            with self.sessions() as db:
                try:
                    yield db
                finally:
                    observations.append(db.in_transaction())

        body = {"email": email, "password": password}
        if chart_id is not None:
            body["guest_chart_id"] = chart_id
        app.dependency_overrides[get_db] = tracked_db
        with ExitStack() as stack:
            stack.enter_context(patch("api.auth.migrate_guest_data_to_user", side_effect=transfer))
            if fail_token:
                stack.enter_context(patch("api.auth.create_access_token", side_effect=RuntimeError("token failure")))
            suffix = "?code=123456" if flow == "verify-code" else ""
            response = self.client.post(f"/auth/{flow}{suffix}", json=body, headers=headers or {})
        self.assertEqual(observations, [False])
        return response, email

    def test_selected_guest_migration_contract_for_both_auth_flows(self):
        for flow in ("login", "verify-code"):
            for scenario in ("only_c", "no_id", "no_header", "wrong_token", "owned", "missing", "full", "last_slot"):
                with self.subTest(flow=flow, scenario=scenario):
                    token = uuid4()
                    ids = [self.chart(user_id=None, session_token=token) for _ in range(3)]
                    selected = ids[2]
                    requested = selected
                    headers = {"X-Session-Token": str(token)}
                    expected = "migrated"
                    saved_count = 0
                    if scenario == "no_id":
                        requested, expected = None, "not_requested"
                    elif scenario == "no_header":
                        headers, expected = {}, "not_requested"
                    elif scenario == "wrong_token":
                        headers, expected = {"X-Session-Token": str(uuid4())}, "not_found"
                    elif scenario == "owned":
                        with self.sessions() as db:
                            db.get(NatalChart, selected).user_id = 2
                            db.commit()
                        expected = "not_found"
                    elif scenario == "missing":
                        requested, expected = 999999, "not_found"
                    elif scenario == "full":
                        saved_count, expected = 3, "limit_reached"
                    elif scenario == "last_slot":
                        saved_count = 2
                    with self.sessions() as db:
                        before = {model: [(row.id, row.chart_id) for row in db.query(model).filter_by(chart_id=selected)]
                                  for model in (ChartData, ChartInterpretationData, GPTMessage)}
                    response, email = self.migration_auth(flow, requested, headers, saved_count)
                    self.assertEqual(response.status_code, 200, response.text)
                    self.assertEqual(response.json()["guest_chart_migration"], {"status": expected, "chart_id": requested})
                    jwt_headers = {"Authorization": "Bearer " + response.json()["access_token"]}
                    self.assertEqual(self.client.get("/auth/me", headers=jwt_headers).status_code, 200)
                    with self.sessions() as db:
                        user = db.query(User).filter_by(email=email).one()
                        chart = db.get(NatalChart, selected)
                        self.assertEqual(chart.user_id, user.id if expected == "migrated" else (2 if scenario == "owned" else None))
                        self.assertEqual(chart.session_token, None if expected == "migrated" else token)
                        self.assertEqual(db.query(NatalChart).filter_by(user_id=user.id).count(), saved_count + (expected == "migrated"))
                        for untouched in ids[:2]:
                            self.assertIsNone(db.get(NatalChart, untouched).user_id)
                            self.assertEqual(db.get(NatalChart, untouched).session_token, token)
                        for model, rows in before.items():
                            self.assertEqual([(row.id, row.chart_id) for row in db.query(model).filter_by(chart_id=selected)], rows)
                    if expected == "migrated":
                        opened = self.client.get(f"/natal-chart/{selected}", headers=jwt_headers)
                        self.assertEqual(opened.status_code, 200, opened.text)
                        self.assertEqual(opened.json()["chart_id"], selected)
                        history = self.client.get(f"/gpt-messages?chart_id={selected}", headers=jwt_headers)
                        self.assertEqual([row["content"] for row in history.json()], ["Existing question", "Existing answer"])

    def test_auth_failure_rolls_back_guest_transfer_for_both_flows(self):
        for flow in ("login", "verify-code"):
            with self.subTest(flow=flow):
                chart_id = self.chart(user_id=None, session_token=self.guest_token)
                response, email = self.migration_auth(flow, chart_id, self.guest, fail_token=True)
                self.assertEqual(response.status_code, 500)
                with self.sessions() as db:
                    chart = db.get(NatalChart, chart_id)
                    self.assertIsNone(chart.user_id)
                    self.assertEqual(chart.session_token, self.guest_token)
                    self.assertEqual(db.query(GPTMessage).filter_by(chart_id=chart_id).count(), 2)
                    if flow == "verify-code":
                        self.assertIsNone(db.query(User).filter_by(email=email).first())
                        self.assertFalse(db.get(EmailVerificationCode, email).used)

    def test_creation_failure_does_not_leave_partial_chart(self):
        def fail_insert(conn, cursor, statement, parameters, context, executemany):
            if statement.startswith("INSERT INTO chart_data"):
                raise RuntimeError("simulated persistence failure")
        event.listen(self.engine, "before_cursor_execute", fail_insert)
        try:
            with self.mock_calculations():
                self.assertEqual(self.client.post("/natal-chart", json=PAYLOAD, headers=self.owner).status_code, 500)
        finally:
            event.remove(self.engine, "before_cursor_execute", fail_insert)
        with self.sessions() as session:
            self.assertEqual(session.query(NatalChart).count(), 0)

    def test_existing_history_and_gpt_endpoint_remain_chart_scoped(self):
        chart_id = self.chart()
        history = self.client.get(f"/gpt-messages?chart_id={chart_id}", headers=self.owner)
        self.assertEqual(history.status_code, 200, history.text)
        self.assertEqual([m["content"] for m in history.json()], ["Existing question", "Existing answer"])
        with patch("api.endpoints.ChartInterpreter") as interpreter:
            from unittest.mock import AsyncMock
            interpreter.return_value.ask_gpt = AsyncMock(return_value="New answer")
            response = self.client.post("/ask-gpt", json={"chart_id": chart_id, "question": "New question"}, headers=self.owner)
            self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(self.client.get(f"/gpt-messages?chart_id={chart_id}", headers=self.owner).json()), 4)
        self.assertEqual(self.client.get(f"/gpt-messages?chart_id={chart_id}", headers=self.other).status_code, 403)
        guest_id = self.chart(user_id=None, session_token=self.guest_token)
        self.assertEqual(self.client.get(f"/gpt-messages?chart_id={guest_id}", headers=self.guest).status_code, 401)
        self.assertEqual(self.client.get(f"/gpt-messages?chart_id={guest_id}", headers={"X-Session-Token": str(uuid4())}).status_code, 401)

    def test_gpt_rejects_missing_invalid_and_expired_jwt_before_openai(self):
        guest_id = self.chart(user_id=None, session_token=self.guest_token)
        expired = create_access_token({"sub": "1"}, expires_delta=timedelta(seconds=-10))
        invalid_tokens = ["invalid", expired, create_access_token({}),
                          create_access_token({"sub": None}), create_access_token({"sub": "not-an-id"})]
        headers_list = [{}, self.guest, {"Authorization": "Basic invalid", **self.guest}]
        for token in invalid_tokens:
            headers_list.extend([{"Authorization": f"Bearer {token}"},
                                 {"Authorization": f"Bearer {token}", **self.guest}])
        before = self.legacy_history(guest_id)
        with patch("api.endpoints.ChartInterpreter") as interpreter, \
                patch("modules.interpretation.client.chat.completions.create") as openai:
            for headers in headers_list:
                with self.subTest(headers=list(headers), authorization=headers.get("Authorization")):
                    response = self.client.post("/ask-gpt", headers=headers,
                                                json={"chart_id": guest_id, "question": "Denied question"})
                    self.assertEqual(response.status_code, 401, response.text)
                    history = self.client.get(f"/gpt-messages?chart_id={guest_id}", headers=headers)
                    self.assertEqual(history.status_code, 401, history.text)
                    self.assertNotIn("Existing answer", history.text)
            interpreter.assert_not_called()
            openai.assert_not_called()
        self.assertEqual(self.legacy_history(guest_id), before)

    def test_gpt_denies_foreign_guest_and_ownerless_charts_even_with_session_token(self):
        ids = [self.chart(user_id=2, session_token=self.guest_token),
               self.chart(user_id=None, session_token=self.guest_token), self.chart(user_id=None)]
        with self.sessions() as db:
            before = db.query(GPTMessage).count()
        with patch("api.endpoints.ChartInterpreter") as interpreter, \
                patch("modules.interpretation.client.chat.completions.create") as openai:
            for chart_id in ids + [999999]:
                for headers in (self.owner, {**self.owner, **self.guest}):
                    with self.subTest(chart_id=chart_id, session="X-Session-Token" in headers):
                        expected = 404 if chart_id == 999999 else 403
                        response = self.client.post("/ask-gpt", headers=headers,
                                                    json={"chart_id": chart_id, "question": "Denied question"})
                        self.assertEqual(response.status_code, expected, response.text)
                        history = self.client.get(f"/gpt-messages?chart_id={chart_id}", headers=headers)
                        self.assertEqual(history.status_code, expected, history.text)
                        self.assertNotIn("Existing answer", history.text)
            interpreter.assert_not_called()
            openai.assert_not_called()
        with self.sessions() as db:
            self.assertEqual(db.query(GPTMessage).count(), before)

    def test_authenticated_gpt_uses_mock_openai_and_persists_both_messages(self):
        chart_id = self.chart()
        reply = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="Mock OpenAI answer"))])
        # Keep the real ask_gpt method, cached prompt and persistence path.
        # The constructor's astrology calculations are unrelated to auth.
        with patch("modules.interpretation.ChartInterpreter.__init__", return_value=None), \
                patch("modules.interpretation.client.chat.completions.create", return_value=reply) as openai:
            response = self.client.post("/ask-gpt", headers={**self.owner, "X-Session-Token": "ignored-invalid-token"},
                                        json={"chart_id": chart_id, "question": "Authenticated question"})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json(), {"chart_id": chart_id, "response": "Mock OpenAI answer"})
            openai.assert_called_once()
        history = self.client.get(f"/gpt-messages?chart_id={chart_id}", headers=self.owner)
        self.assertEqual(history.status_code, 200, history.text)
        self.assertEqual([(m["role"], m["content"]) for m in history.json()][-2:],
                         [("user", "Authenticated question"), ("gpt", "Mock OpenAI answer")])


if __name__ == "__main__":
    unittest.main()
