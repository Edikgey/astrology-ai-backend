"""BE-HYGIENE regression (tasks/BE-HYGIENE/DESIGN.md §5, R1-R7).

R1  removed /chart-bodies-info and /get-patterns answer 404 for everyone.
R2  GET /health: 200 {"status": "ok"}, no-store, no auth, no environment details.
R3  GET /health never opens a database connection.
R4  database/reset_db.py refuses by default (exit 2, Т2) and prints no address or password.
R5  reset_db.main() refuses every non-disposable target (Т1) and every missing/wrong
    confirmation (Т2) before any connection is opened.
R6  importing database.reset_db does nothing (no connection, no .env, no database.connection).
R7  real disposable PostgreSQL database: refusal keeps data; explicit confirmation clears it.

SAFETY. The pre-fix script drops schema public at import/run time and, without
DATABASE_URL, reads a .env found upward from database/. Therefore:
- every subprocess gets an explicit DATABASE_URL that _run_python() re-validates as a
  local PostgreSQL database named test_*/*_test without query parameters (a closed
  port 127.0.0.1:9 or the stand's own throwaway database test_reset_<hex>); there is
  no code path that starts a subprocess without it;
- libpq redirection variables, the stand URL and any confirmation are stripped from the
  subprocess environment; the subprocess cwd is a fresh temporary directory;
- in-process checks (including "DATABASE_URL absent") run with Engine.connect and
  Engine.raw_connection patched to raise, so no connection can be opened at all;
- database/ is never put on sys.path; the shared stand database test_usage is never
  targeted; messages never contain a URL with a password.
"""
import importlib
import io
import os
import socket
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

import test_my_charts as fixtures  # isolated app import: sqlite, dummy keys, no .env


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "database" / "reset_db.py"
MODULE = "database.reset_db"
LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")
SHARED_STAND_DB = "test_usage"
SUBPROCESS_TIMEOUT = 60  # seconds, wall clock of the test process; a hung child is killed
CLOSED_PORT = 9
# Never inherited by a reset_db.py subprocess: confirmation, libpq redirection/credentials,
# the stand URL and any ambient DATABASE_URL (always replaced by an explicit one).
SCRUBBED_ENV = ("LUNARIA_RESET_DB_CONFIRM", "DATABASE_URL", "USAGE_TEST_POSTGRES_URL",
                "PGPASSWORD", "PGPASSFILE", "PGHOSTADDR", "PGHOST", "PGPORT", "PGUSER",
                "PGDATABASE", "PGSERVICE", "PGSERVICEFILE")

T1 = ("Отказ: этот скрипт удаляет все таблицы и данные. Он работает только с одноразовой базой "
      "PostgreSQL на этом компьютере: в DATABASE_URL адрес 127.0.0.1, localhost или ::1, имя базы "
      "начинается с test_ или заканчивается на _test, без параметров после «?».")


def t2(name):
    return (f"Отказ: будут удалены все таблицы и данные базы «{name}». Если эта база одноразовая, "
            f"запустите снова, задав переменную окружения LUNARIA_RESET_DB_CONFIRM={name}.")


def _is_disposable_target(raw):
    """The only targets a reset_db.py subprocess may ever receive (stricter than the script)."""
    try:
        url = make_url(raw)
    except Exception:
        return False
    name = url.database or ""
    return (url.get_backend_name() == "postgresql" and url.host in LOCAL_HOSTS and not url.query
            and (name.startswith("test_") or name.endswith("_test")) and name != SHARED_STAND_DB)


def _text(raw):
    return raw.decode("utf-8", errors="replace").replace("\r\n", "\n").rstrip("\n")


def _redact(value, secrets):
    for secret in secrets:
        if secret:
            value = value.replace(secret, "<redacted>")
    return value[-1500:]


def _run_python(argv, database_url, confirm=None):
    """Run the interpreter with an explicit, re-validated DATABASE_URL. No other way exists."""
    if not database_url or not _is_disposable_target(database_url):
        raise RuntimeError("refusing to start a subprocess without an explicit disposable local DATABASE_URL")
    env = {key: value for key, value in os.environ.items() if key.upper() not in SCRUBBED_ENV}
    env.update(DATABASE_URL=database_url, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    if confirm is not None:
        env["LUNARIA_RESET_DB_CONFIRM"] = confirm
    with tempfile.TemporaryDirectory(prefix="reset_db_guard_") as cwd:
        proc = subprocess.run([sys.executable, *argv], cwd=cwd, env=env, capture_output=True,
                              timeout=SUBPROCESS_TIMEOUT)
    return proc.returncode, _text(proc.stdout), _text(proc.stderr)


def _assert_closed_port(case):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(3)
        if sock.connect_ex(("127.0.0.1", CLOSED_PORT)) == 0:
            case.fail(f"127.0.0.1:{CLOSED_PORT} accepts connections; the closed-port premise does not hold")


class BeHygieneHttpTests(unittest.TestCase):
    """R1-R3 on the isolated API fixture (sqlite in memory, no .env)."""

    def setUp(self):
        fixtures.MyChartsTests.setUp(self)
    tearDown = fixtures.MyChartsTests.tearDown
    chart = fixtures.MyChartsTests.chart

    def test_r1_removed_chart_endpoints_answer_404_for_everyone(self):
        own_chart = self.chart(session_token=self.guest_token)
        callers = {"owner_jwt": self.owner, "other_jwt": self.other, "guest_token": self.guest, "anonymous": {}}
        for path in ("/chart-bodies-info", "/get-patterns"):
            for caller, headers in callers.items():
                with self.subTest(path=path, caller=caller, method="POST"):
                    response = self.client.post(path, json={"chart_id": own_chart}, headers=headers)
                    self.assertEqual(response.status_code, 404)
                    self.assertEqual(response.json(), {"detail": "Not Found"})
            with self.subTest(path=path, caller="owner_jwt", method="GET"):
                response = self.client.get(path, headers=self.owner)
                self.assertEqual(response.status_code, 404)
                self.assertEqual(response.json(), {"detail": "Not Found"})

    def test_r2_health_is_public_constant_and_uncached(self):
        token = uuid4().hex
        canaries = {
            "RAILWAY_GIT_COMMIT_SHA": "sha" + token,
            "RAILWAY_ENVIRONMENT_NAME": "env" + token,
            "DATABASE_URL": f"postgresql://canary_user:pw{token}@host{token}.invalid:5432/db{token}",
        }
        with patch.dict(os.environ, canaries):
            response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})
        self.assertEqual(response.headers.get("cache-control"), "no-store")
        exposed = response.text + "\n" + "\n".join(f"{k}: {v}" for k, v in response.headers.items())
        self.assertNotIn(token, exposed, "an environment value leaked into /health")

    def test_r3_health_never_touches_the_database(self):
        with patch("sqlalchemy.engine.Engine.connect", side_effect=AssertionError("database used")) as connect, \
                patch("sqlalchemy.engine.Engine.raw_connection", side_effect=AssertionError("database used")) as raw:
            response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})
        connect.assert_not_called()
        raw.assert_not_called()


class ResetDbGuardTests(unittest.TestCase):
    """R4-R6 without any reachable database."""

    def setUp(self):
        self.token = uuid4().hex
        self.password = "pw" + self.token
        # Ambient value for anything in this process: a closed local port, never a real database.
        env = patch.dict(os.environ, {"DATABASE_URL": self.closed_url("test_reset_guard")})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("LUNARIA_RESET_DB_CONFIRM", None)
        self.connect = self.start(patch("sqlalchemy.engine.Engine.connect",
                                        side_effect=AssertionError("reset_db opened a connection")))
        self.raw = self.start(patch("sqlalchemy.engine.Engine.raw_connection",
                                    side_effect=AssertionError("reset_db opened a connection")))
        sys.modules.pop(MODULE, None)
        self.addCleanup(sys.modules.pop, MODULE, None)

    def start(self, patcher):
        mocked = patcher.start()
        self.addCleanup(patcher.stop)
        return mocked

    def closed_url(self, database, host="127.0.0.1", scheme="postgresql", user=None):
        user = user or "guard_user_" + self.token[:8]
        return f"{scheme}://{user}:{self.password}@{host}:{CLOSED_PORT}/{database}"

    def assert_no_secrets(self, *outputs):
        for output in outputs:
            self.assertNotIn(self.password, output, "password printed")
            self.assertNotIn("guard_user_", output, "user name printed")
            self.assertNotIn("Traceback", output)

    def call_main(self, module, env):
        out, err = io.StringIO(), io.StringIO()
        with patch.dict(os.environ), redirect_stdout(out), redirect_stderr(err):
            os.environ.pop("DATABASE_URL", None)
            os.environ.pop("LUNARIA_RESET_DB_CONFIRM", None)
            os.environ.update(env)
            try:
                code = module.main()
            except SystemExit as exit_:
                code = exit_.code
        return code, out.getvalue().replace("\r\n", "\n").rstrip("\n"), err.getvalue().replace("\r\n", "\n").rstrip("\n")

    def import_fresh(self):
        sys.modules.pop(MODULE, None)
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            module = importlib.import_module(MODULE)
        return module, out.getvalue(), err.getvalue()

    def test_r4_script_refuses_without_confirmation_and_prints_no_address(self):
        _assert_closed_port(self)
        code, out, err = _run_python([str(SCRIPT)], self.closed_url("test_reset_guard"))
        detail = f"exit={code} stderr_tail={_redact(err, [self.password])!r}"
        self.assertEqual(code, 2, detail)
        self.assertEqual(out, "", detail)
        self.assertEqual(err, t2("test_reset_guard"), detail)
        self.assert_no_secrets(out, err)
        self.assertNotIn("127.0.0.1", out + err, "host printed")

    def test_r5a_wrong_target_is_refused_before_connecting(self):
        module, _, _ = self.import_fresh()
        cases = {
            "remote_host": (self.closed_url("test_x", host="db.example.invalid"), "test_x"),
            "local_lookalike_host": (self.closed_url("test_x", host="localhost.example.invalid"), "test_x"),
            "query_host_redirect": (self.closed_url("test_x") + "?host=db.example.invalid", "test_x"),
            "query_hostaddr_redirect": (self.closed_url("test_x") + "?hostaddr=192.0.2.1", "test_x"),
            "non_test_name": (self.closed_url("lunaria"), "lunaria"),
            "empty_name": (self.closed_url(""), ""),
            "no_host": ("postgresql:///test_x", "test_x"),
            "postgres_scheme": (f"postgres://guard_user_x:{self.password}@localhost/test_x", "test_x"),
            "sqlite": ("sqlite://", "test_x"),
            "empty_string": ("", "test_x"),
            "variable_absent": (None, "test_x"),
        }
        for case, (url, confirm) in cases.items():
            with self.subTest(case=case):
                env = {"LUNARIA_RESET_DB_CONFIRM": confirm}
                if url is not None:
                    env["DATABASE_URL"] = url
                code, out, err = self.call_main(module, env)
                self.assertEqual(code, 2)
                self.assertEqual(out, "")
                self.assertEqual(err, T1)
                self.assert_no_secrets(out, err)
        self.connect.assert_not_called()
        self.raw.assert_not_called()

    def test_r5b_missing_or_wrong_confirmation_is_refused_before_connecting(self):
        module, _, _ = self.import_fresh()
        cases = [
            (self.closed_url("test_x"), None, "test_x"),
            (self.closed_url("test_x"), "", "test_x"),
            (self.closed_url("test_x"), "yes", "test_x"),
            (self.closed_url("test_x"), "test_other", "test_x"),
            (self.closed_url("test_x"), "TEST_X", "test_x"),
            (self.closed_url("lunaria_test", host="localhost", scheme="postgresql+psycopg2"), None, "lunaria_test"),
            (self.closed_url("test_x", host="[::1]"), "test_other", "test_x"),
        ]
        for url, confirm, name in cases:
            with self.subTest(name=name, confirm=confirm, host=make_url(url).host):
                env = {"DATABASE_URL": url}
                if confirm is not None:
                    env["LUNARIA_RESET_DB_CONFIRM"] = confirm
                code, out, err = self.call_main(module, env)
                self.assertEqual(code, 2)
                self.assertEqual(out, "")
                self.assertEqual(err, t2(name))
                self.assert_no_secrets(out, err)
        self.connect.assert_not_called()
        self.raw.assert_not_called()

    def test_r6_import_does_nothing(self):
        module, out, err = self.import_fresh()
        self.assertTrue(callable(getattr(module, "main", None)), "reset_db.main() is missing")
        self.assertEqual((out, err), ("", ""))
        self.connect.assert_not_called()
        self.raw.assert_not_called()

    def test_r6_import_in_clean_interpreter_reads_no_env_file_and_no_app_connection(self):
        _assert_closed_port(self)
        probe = ("import sys; sys.path.insert(0, sys.argv[1]); import database.reset_db; "
                 "print(','.join(sorted(m for m in sys.modules if m in ('dotenv', 'connection', "
                 "'database.connection') or m.startswith('dotenv.'))))")
        code, out, err = _run_python(["-I", "-c", probe, str(ROOT)], self.closed_url("test_reset_guard"))
        detail = f"exit={code} stderr_tail={_redact(err, [self.password])!r}"
        self.assertEqual(code, 0, detail)
        self.assertEqual(err, "", detail)
        self.assertEqual(out, "", f"imported at module import time: {out!r}")


@unittest.skipUnless(os.getenv("USAGE_TEST_POSTGRES_URL"), "Local PostgreSQL test URL not configured")
class ResetDbPostgresTests(unittest.TestCase):
    """R7 on a throwaway database test_reset_<hex> in the stand's own PostgreSQL cluster."""

    def setUp(self):
        stand = make_url(os.environ["USAGE_TEST_POSTGRES_URL"])
        stand_name = stand.database or ""
        # Same rule as tests/test_postgres_usage.py:27-29.
        if (stand.get_backend_name() != "postgresql" or stand.host not in LOCAL_HOSTS or
                not (stand_name.startswith("test_") or stand_name.endswith("_test"))):
            raise RuntimeError("Only an explicitly named local test database is allowed")
        self.secrets = [stand.password or ""]
        self.name = "test_reset_" + uuid4().hex
        self.url = stand.set(database=self.name)
        self.admin = create_engine(stand, isolation_level="AUTOCOMMIT")
        self.addCleanup(self.admin.dispose)
        with self.admin.connect() as conn:
            conn.exec_driver_sql(f'CREATE DATABASE "{self.name}"')
        self.addCleanup(self.drop_database)
        target = create_engine(self.url)
        try:
            with target.begin() as conn:
                conn.exec_driver_sql("CREATE TABLE public.reset_marker (id integer PRIMARY KEY)")
                conn.exec_driver_sql("INSERT INTO public.reset_marker (id) VALUES (1)")
        finally:
            target.dispose()
        self.assertEqual(self.state(), (True, True))

    def drop_database(self):
        with self.admin.connect() as conn:
            conn.exec_driver_sql(f'DROP DATABASE IF EXISTS "{self.name}" WITH (FORCE)')

    def state(self):
        """(marker table present, schema public present) in the throwaway database."""
        engine = create_engine(self.url)
        try:
            with engine.connect() as conn:
                marker = conn.execute(text("SELECT to_regclass('public.reset_marker') IS NOT NULL")).scalar()
                public = conn.execute(text(
                    "SELECT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = 'public')")).scalar()
        finally:
            engine.dispose()
        return bool(marker), bool(public)

    def run_script(self, confirm=None):
        url = self.url.render_as_string(hide_password=False)
        code, out, err = _run_python([str(SCRIPT)], url, confirm=confirm)
        for output in (out, err):
            for secret in self.secrets:
                if secret:
                    self.assertNotIn(secret, output, "password printed")
        return code, out, err, f"exit={code} stderr_tail={_redact(err, self.secrets)!r}"

    def test_r7a_without_confirmation_the_data_stays(self):
        code, out, err, detail = self.run_script()
        self.assertEqual(self.state(), (True, True), f"refused run must leave the data in place; {detail}")
        self.assertEqual(code, 2, detail)
        self.assertEqual(out, "", detail)
        self.assertEqual(err, t2(self.name), detail)

    def test_r7b_with_explicit_confirmation_the_schema_is_cleared(self):
        code, out, err, detail = self.run_script(confirm=self.name)
        self.assertEqual(code, 0, detail)
        self.assertEqual(self.state(), (False, True), "confirmed run must drop the data and recreate public")


if __name__ == "__main__":
    unittest.main()
