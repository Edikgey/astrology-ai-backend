"""BE-AUTH-GUEST R6: a real PostgreSQL connection killed under the session check; the server recovers by itself."""
import os
import unittest
from uuid import uuid4

from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

import test_my_charts as fixtures
from test_auth_guest import UNAVAILABLE
from database.queries import User


@unittest.skipUnless(os.getenv("USAGE_TEST_POSTGRES_URL"), "Local PostgreSQL test URL not configured")
class SessionCheckPostgresTests(unittest.TestCase):
    chart = fixtures.MyChartsTests.chart

    def setUp(self):
        url = make_url(os.environ["USAGE_TEST_POSTGRES_URL"])
        if url.get_backend_name() != "postgresql" or url.host not in ("localhost", "127.0.0.1", "::1") \
                or not (url.database or "").startswith("test_"):
            raise RuntimeError("Only a local test database is allowed")
        fixtures.MyChartsTests.setUp(self)  # SQLite stand and API overrides; the engine is replaced below
        self.engine.dispose()
        self.schema = "auth_guest_" + uuid4().hex
        self.app_name = "auth_guest_" + uuid4().hex[:12]
        self.admin = create_engine(url)
        with self.admin.begin() as conn:
            conn.exec_driver_sql(f'CREATE SCHEMA "{self.schema}"')
        self.addCleanup(self.cleanup_schema)
        self.engine = create_engine(url, connect_args={"options": f"-csearch_path={self.schema}",
                                                       "application_name": self.app_name})
        fixtures.Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine, autoflush=False)  # read by the test_db override at request time
        with self.sessions() as db:
            db.add_all([User(id=1, email="a@example.test", password_hash="unused"),
                        User(id=2, email="b@example.test", password_hash="unused")])
            db.commit()

    def tearDown(self):
        fixtures.MyChartsTests.tearDown(self)

    def cleanup_schema(self):
        self.engine.dispose()
        with self.admin.begin() as conn:
            conn.exec_driver_sql(f'DROP SCHEMA "{self.schema}" CASCADE')
        self.admin.dispose()

    def kill_test_connections(self):
        with self.admin.begin() as conn:
            rows = conn.exec_driver_sql(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE application_name = %s AND pid <> pg_backend_pid()", (self.app_name,)).fetchall()
        return len(rows)

    def test_r6_killed_connection_gives_503_then_the_next_request_succeeds(self):
        own_id = self.chart()
        self.assertEqual(self.client.get("/auth/me", headers=self.owner).status_code, 200)  # warm the pool
        self.assertGreaterEqual(self.kill_test_connections(), 1, "the test must kill a real pooled connection")
        down = self.client.get(f"/natal-chart/{own_id}", headers=self.owner)
        self.assertEqual(down.status_code, 503, down.text)
        self.assertEqual(down.json()["detail"], UNAVAILABLE)
        again = self.client.get(f"/natal-chart/{own_id}", headers=self.owner)
        self.assertEqual(again.status_code, 200, again.text)  # nobody signed out; the pool replaced the dead connection


if __name__ == "__main__":
    unittest.main()
