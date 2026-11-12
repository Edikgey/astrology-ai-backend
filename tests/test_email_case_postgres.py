"""BE-EMAIL-CASE on real PostgreSQL: every SQLite case plus the registration race (T9).

Runs only against the disposable loopback database of scripts/test-backend.ps1 (own schema,
Base.metadata.create_all, dropped afterwards). Without USAGE_TEST_POSTGRES_URL the class is
skipped - a skipped run is not a pass.

Interleaving is forced, not hoped for: both verify-code requests meet at a two-party barrier
inside api.auth.get_password_hash, i.e. after the code was accepted and before the account
row is written, so both INSERTs are in flight together and the unique index has to decide.
"""
import logging
import os
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from uuid import uuid4

from sqlalchemy import create_engine, inspect
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

import test_email_case as cases
from api.auth import get_password_hash, verify_password
from database.connection import Base
from database.queries import User

BARRIER_TIMEOUT_SECONDS = 10  # bcrypt + one round trip on loopback is well under 1 s
RESULT_TIMEOUT_SECONDS = 60   # hard bound so a deadlock fails the test instead of hanging the run


@unittest.skipUnless(os.getenv('USAGE_TEST_POSTGRES_URL'), 'Local PostgreSQL test URL not configured')
class EmailCasePostgresTests(cases.EmailCaseTests):
    def setUp(self):
        url = make_url(os.environ['USAGE_TEST_POSTGRES_URL'])
        if url.get_backend_name() != 'postgresql' or url.host not in ('localhost', '127.0.0.1', '::1') \
                or not (url.database.startswith('test_') or url.database.endswith('_test')):
            raise RuntimeError('Only a local named test database is allowed')
        super().setUp()
        self.engine.dispose()
        self.schema = 'email_case_' + uuid4().hex
        self.admin = create_engine(url)
        with self.admin.begin() as conn:
            conn.exec_driver_sql(f'CREATE SCHEMA "{self.schema}"')
        self.addCleanup(self.clean_schema)
        self.engine = create_engine(url, connect_args={'options': f'-csearch_path={self.schema}'})
        self.sessions = sessionmaker(bind=self.engine, autoflush=False)
        # Fresh schema, no seeded users: the users.id sequence starts clean, so no test can
        # pass through a primary-key collision.
        Base.metadata.create_all(self.engine)

    def clean_schema(self):
        self.engine.dispose()
        with self.admin.begin() as conn:
            conn.exec_driver_sql(f'DROP SCHEMA "{self.schema}" CASCADE')
        self.admin.dispose()

    def test_t9_concurrent_verify_in_two_cases_creates_one_account(self):
        """T9: Race.User@ and race.user@ verified at the same moment -> [200, 400], one account, no 500."""
        with self.engine.connect() as conn:
            indexes = inspect(conn).get_indexes('users')
        self.assertTrue(any(ix['unique'] and ix['column_names'] == ['email'] for ix in indexes), indexes)

        self.assertEqual(self.request_register('Race.User@example.com').status_code, 200)
        self.assertEqual(self.request_register('race.user@example.com', later_seconds=61).status_code, 200)

        barrier = threading.Barrier(2, timeout=BARRIER_TIMEOUT_SECONDS)
        arrivals = []

        def hash_at_barrier(password):
            arrivals.append(threading.get_ident())
            try:
                barrier.wait()
            except threading.BrokenBarrierError:
                # A fix that serialises earlier (e.g. a row lock) leaves one request waiting
                # before this point; then the outcome below still has to hold.
                pass
            return get_password_hash(password)

        with patch('api.auth.get_password_hash', hash_at_barrier), ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(self.verify, email)
                       for email in ('Race.User@example.com', 'race.user@example.com')]
            responses = [future.result(timeout=RESULT_TIMEOUT_SECONDS) for future in futures]

        statuses = sorted(r.status_code for r in responses)
        self.assertEqual(statuses, [200, 400], [r.text for r in responses])
        refused = next(r for r in responses if r.status_code == 400)
        self.assertEqual(refused.json()['detail'], cases.DUPLICATE)
        winner = next(r for r in responses if r.status_code == 200)
        stored = self.accounts('race.user@example.com')
        self.assertEqual(len(stored), 1, stored)
        self.assertEqual(stored[0][1], 'race.user@example.com')
        self.assertEqual(self.me(winner.json()['access_token'])['id'], stored[0][0])

    def test_t9b_competing_insert_after_checks_is_400_not_500_and_not_logged(self):
        """T9b: a row for the same address commits after the checks and before the INSERT.

        Deterministic single-request version of the race: the unique violation must end as
        the fixed 400 text (not 500), and PostgreSQL's "Key (email)=(...)" must not reach logs.
        """
        self.assertEqual(self.request_register('Race2.User@example.com').status_code, 200)

        def hash_after_competitor(password):
            with self.sessions() as db:
                db.add(User(email='race2.user@example.com', password_hash=get_password_hash(cases.OTHER_PASSWORD)))
                db.commit()
            return get_password_hash(password)

        with patch('api.auth.get_password_hash', hash_after_competitor), self.assertLogs(level='DEBUG') as logs:
            logging.getLogger('be_email_case').debug(cases.LOG_MARKER)
            response = self.verify('Race2.User@example.com')
        self.assert_duplicate(response)
        stored = self.accounts('race2.user@example.com')
        self.assertEqual(len(stored), 1, stored)
        self.assertTrue(verify_password(cases.OTHER_PASSWORD, stored[0][2]))
        captured = '\n'.join(logs.output)
        self.assertIn(cases.LOG_MARKER, captured)
        self.assertNotIn('race2.user', captured.casefold())
        self.assertNotIn('race2.user', response.text.casefold())


if __name__ == '__main__':
    unittest.main()
