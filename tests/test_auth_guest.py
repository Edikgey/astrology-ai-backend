"""BE-AUTH-GUEST: a database failure during the session check must not look like "you are not signed in".

R1-R5, R7: regression (red on 90e9f2b). G1-G3: guards for the behaviour that must stay as it is.
Synthetic data only; SQLite in memory; no network. The real-PostgreSQL case R6 is in test_auth_guest_postgres.py.
"""
import asyncio
import base64
import json
import logging
import time
import unittest
from contextlib import contextmanager
from datetime import timedelta
from uuid import uuid4

from jose import jwt
from sqlalchemy import event
from sqlalchemy.exc import OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError

import test_my_charts as fixtures
from api import auth
from api.endpoints import CHART_NOT_FOUND
from database.queries import NatalChart, User

UNAVAILABLE = "Сервис временно недоступен: не удалось проверить вход. Подождите минуту и повторите."
NOT_SIGNED_IN = "Войдите в аккаунт или обновите страницу, чтобы продолжить."
SECRET_MARKER = "synthetic-secret-marker-7f3a"


def users_lookup_error(kind):
    """The exception a broken connection or an exhausted pool would raise for the lookup of a user."""
    if kind == "operational":
        return OperationalError("SELECT users.id FROM users", {}, Exception("db down " + SECRET_MARKER))
    if kind == "pool_timeout":
        return PoolTimeoutError("QueuePool limit of size 5 overflow 10 reached " + SECRET_MARKER)
    return RuntimeError("not a database error " + SECRET_MARKER)


@contextmanager
def users_lookup_fails(engine, error_factory):
    """Fail every SELECT from `users` the way a database outage would; other tables keep working."""
    def fail(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT") and "FROM users" in statement:
            raise error_factory()
    event.listen(engine, "before_cursor_execute", fail)
    try:
        yield
    finally:
        event.remove(engine, "before_cursor_execute", fail)


class AuthGuestBase(unittest.TestCase):
    """Reuses the my-charts stand (users 1 and 2, owner/other JWT, guest header) without re-running its tests."""
    chart = fixtures.MyChartsTests.chart
    mock_calculations = fixtures.MyChartsTests.mock_calculations

    def setUp(self):
        fixtures.MyChartsTests.setUp(self)

    def tearDown(self):
        fixtures.MyChartsTests.tearDown(self)

    def jwt_for(self, sub="1", **claims):
        return {"Authorization": "Bearer " + jwt.encode(
            {"sub": sub, "exp": int(time.time()) + 300, **claims}, auth.SECRET_KEY, algorithm=auth.ALGORITHM)}

    def chart_rows(self):
        with self.sessions() as db:
            return db.query(NatalChart).count()

    def assert_unavailable(self, response):
        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(response.headers["content-type"].split(";")[0], "application/json")
        self.assertEqual(response.json()["detail"], UNAVAILABLE)


class SessionCheckFailureTests(AuthGuestBase):
    def test_r1_get_own_chart_database_failure_is_503_not_401(self):
        own_id = self.chart()
        for kind in ("operational", "pool_timeout"):
            with self.subTest(kind=kind), users_lookup_fails(self.engine, lambda: users_lookup_error(kind)):
                self.assert_unavailable(self.client.get(f"/natal-chart/{own_id}", headers=self.owner))

    def test_r1_post_chart_database_failure_is_503_not_401(self):
        before = self.chart_rows()
        for kind in ("operational", "pool_timeout"):
            with self.subTest(kind=kind), users_lookup_fails(self.engine, lambda: users_lookup_error(kind)), self.mock_calculations():
                self.assert_unavailable(self.client.post("/natal-chart", json=fixtures.PAYLOAD, headers=self.owner))
        self.assertEqual(self.chart_rows(), before)

    def test_r2_get_with_guest_header_database_failure_is_503_not_404(self):
        own_id = self.chart()
        for kind in ("operational", "pool_timeout"):
            with self.subTest(kind=kind), users_lookup_fails(self.engine, lambda: users_lookup_error(kind)):
                self.assert_unavailable(self.client.get(f"/natal-chart/{own_id}", headers={**self.owner, **self.guest}))

    def test_r2_post_with_guest_header_database_failure_is_503_and_creates_no_chart(self):
        before = self.chart_rows()
        for kind in ("operational", "pool_timeout"):
            with self.subTest(kind=kind), users_lookup_fails(self.engine, lambda: users_lookup_error(kind)), self.mock_calculations():
                self.assert_unavailable(self.client.post(
                    "/natal-chart", json=fixtures.PAYLOAD, headers={**self.owner, **self.guest}))
                self.assertEqual(self.chart_rows(), before, "no chart may be created while the session check is down")

    def test_r3_get_current_user_database_failure_is_503_json_in_russian(self):
        self.chart()
        for kind in ("operational", "pool_timeout"):
            for path in ("/auth/me", "/natal-charts"):
                with self.subTest(kind=kind, path=path), users_lookup_fails(self.engine, lambda: users_lookup_error(kind)):
                    self.assert_unavailable(self.client.get(path, headers=self.owner))

    def test_r4_a_non_database_error_is_not_swallowed(self):
        own_id = self.chart()
        for headers in (self.owner, {**self.owner, **self.guest}):
            with self.subTest(headers=sorted(headers)), users_lookup_fails(self.engine, lambda: users_lookup_error("other")):
                response = self.client.get(f"/natal-chart/{own_id}", headers=headers)
                self.assertEqual(response.status_code, 500, response.text)

    def test_r5_one_log_line_with_the_error_class_and_no_secrets(self):
        own_id = self.chart()
        records = []

        class Collect(logging.Handler):
            def emit(self, record):
                records.append(record)

        root = logging.getLogger()
        handler, level = Collect(), root.level
        root.addHandler(handler)
        root.setLevel(logging.WARNING)
        try:
            token = self.owner["Authorization"].split(" ", 1)[1]
            with users_lookup_fails(self.engine, lambda: users_lookup_error("operational")):
                response = self.client.get(f"/natal-chart/{own_id}", headers=self.owner)
        finally:
            root.removeHandler(handler)
            root.setLevel(level)
        ours = [r for r in records if r.name == "api.auth" and r.levelno == logging.WARNING]
        self.assertEqual([r.getMessage() for r in ours], ["Session check unavailable (OperationalError)"])
        formatter = logging.Formatter()
        for record in records:
            rendered = formatter.format(record) + repr(record.args)
            self.assertNotIn(SECRET_MARKER, rendered)
            self.assertNotIn(token, rendered)
        self.assert_unavailable(response)


class SessionCheckOffTheEventLoopTests(AuthGuestBase):
    def lookup_loop_flags(self, path, headers):
        flags = []

        def observe(conn, cursor, statement, parameters, context, executemany):
            if statement.lstrip().upper().startswith("SELECT") and "FROM users" in statement:
                try:
                    asyncio.get_running_loop()
                    flags.append(True)
                except RuntimeError:
                    flags.append(False)

        event.listen(self.engine, "before_cursor_execute", observe)
        try:
            response = self.client.get(path, headers=headers)
        finally:
            event.remove(self.engine, "before_cursor_execute", observe)
        return response, flags

    def test_r7_user_lookup_in_get_current_user_or_guest_runs_outside_the_event_loop(self):
        own_id = self.chart()
        # Control: get_current_user is a plain `def`, so its lookup already runs in a worker thread.
        response, flags = self.lookup_loop_flags("/auth/me", self.owner)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(flags, [False])
        response, flags = self.lookup_loop_flags(f"/natal-chart/{own_id}", self.owner)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(flags, [False], "the blocking user lookup must not run inside the event loop")


class BadTokenGuards(AuthGuestBase):
    """G1-G3: answers that must stay exactly as they are."""

    def bad_tokens(self):
        def part(data):
            return base64.urlsafe_b64encode(json.dumps(data, separators=(",", ":")).encode()).rstrip(b"=").decode()
        unsigned = part({"alg": "none", "typ": "JWT"}) + "." + part({"sub": "1", "exp": int(time.time()) + 300}) + "."
        expired = auth.create_access_token({"sub": "1"}, expires_delta=timedelta(minutes=-5))
        foreign = jwt.encode({"sub": "1", "exp": int(time.time()) + 300}, "some-other-secret", algorithm="HS256")
        no_sub = jwt.encode({"exp": int(time.time()) + 300}, auth.SECRET_KEY, algorithm="HS256")
        return {"garbage": "not-a-jwt", "foreign_secret": foreign, "expired": expired, "alg_none": unsigned,
                "sub_not_a_number": jwt.encode({"sub": "abc", "exp": int(time.time()) + 300}, auth.SECRET_KEY, algorithm="HS256"),
                "sub_missing": no_sub}

    def stale_identities(self):
        with self.sessions() as db:
            db.get(User, 2).auth_version = 1  # user 2 signed out everywhere; the next token still carries ver 0
            db.commit()
        return {"deleted_user": self.jwt_for("99"), "old_version": self.jwt_for("2", ver=0)}

    def test_g1_bad_token_without_guest_header_is_401_with_the_old_text(self):
        own_id = self.chart()
        for name, token in self.bad_tokens().items():
            headers = {"Authorization": "Bearer " + token}
            with self.subTest(token=name):
                get = self.client.get(f"/natal-chart/{own_id}", headers=headers)
                self.assertEqual((get.status_code, get.json()["detail"]), (401, NOT_SIGNED_IN))
                with self.mock_calculations():
                    post = self.client.post("/natal-chart", json=fixtures.PAYLOAD, headers=headers)
                self.assertEqual((post.status_code, post.json()["detail"]), (401, NOT_SIGNED_IN))

    def assert_guest_path(self, headers, own_id):
        # A fresh guest per case (guest creation is rate limited): the guest's own chart opens, the account's
        # chart stays hidden, and a new chart is created as a guest chart.
        token = uuid4()
        guest_id = self.chart(user_id=None, session_token=token)
        guest = {"X-Session-Token": str(token)}
        self.assertEqual(self.client.get(f"/natal-chart/{guest_id}", headers={**headers, **guest}).status_code, 200)
        hidden = self.client.get(f"/natal-chart/{own_id}", headers={**headers, **guest})
        self.assertEqual((hidden.status_code, hidden.json()["detail"]), (404, CHART_NOT_FOUND))
        before = self.chart_rows()
        with self.mock_calculations():
            created = self.client.post("/natal-chart", json=fixtures.PAYLOAD, headers={**headers, **guest})
        self.assertEqual(created.status_code, 200, created.text)
        with self.sessions() as db:
            self.assertEqual(db.query(NatalChart).count(), before + 1)
            newest = db.query(NatalChart).order_by(NatalChart.id.desc()).first()
            self.assertEqual((newest.user_id, newest.session_token), (None, token))

    def test_g2_bad_token_with_guest_header_takes_the_guest_path(self):
        own_id = self.chart()
        for name, token in self.bad_tokens().items():
            with self.subTest(token=name):
                self.assert_guest_path({"Authorization": "Bearer " + token}, own_id)

    def test_g3_deleted_user_and_old_version_tokens_behave_like_bad_tokens(self):
        own_id = self.chart(user_id=2)
        for name, headers in self.stale_identities().items():
            with self.subTest(token=name):
                without = self.client.get(f"/natal-chart/{own_id}", headers=headers)
                self.assertEqual((without.status_code, without.json()["detail"]), (401, NOT_SIGNED_IN))
                self.assert_guest_path(headers, own_id)


if __name__ == "__main__":
    unittest.main()
