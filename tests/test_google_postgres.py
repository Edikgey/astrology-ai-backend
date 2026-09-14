"""Local PostgreSQL migration/locking checks using the existing guarded test URL."""
import os
import unittest
from pathlib import Path
from uuid import uuid4
from concurrent.futures import ThreadPoolExecutor
from sqlalchemy import create_engine, MetaData, Table, inspect
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker
from sqlalchemy.exc import IntegrityError
import test_google_auth as fixtures
from database.connection import Base
from database.queries import User


@unittest.skipUnless(os.getenv('USAGE_TEST_POSTGRES_URL'), 'Local PostgreSQL test URL not configured')
class GooglePostgresTests(fixtures.GoogleAuthTests):
    def setUp(self):
        url = make_url(os.environ['USAGE_TEST_POSTGRES_URL'])
        if url.get_backend_name() != 'postgresql' or url.host not in ('localhost', '127.0.0.1', '::1') or not (url.database.startswith('test_') or url.database.endswith('_test')):
            raise RuntimeError('Only a local named test database is allowed')
        super().setUp()
        self.engine.dispose()
        self.schema = 'google_test_' + uuid4().hex
        self.admin = create_engine(url)
        with self.admin.begin() as conn: conn.exec_driver_sql(f'CREATE SCHEMA "{self.schema}"')
        self.addCleanup(self.clean_schema)
        self.engine = create_engine(url, connect_args={'options': f'-csearch_path={self.schema}'})
        self.sessions = sessionmaker(bind=self.engine, autoflush=False)
        legacy = MetaData()
        for table in Base.metadata.sorted_tables:
            if table.name == 'users': Table('users', legacy, *(c._copy() for c in table.columns if c.name != 'google_sub'))
            else: table.to_metadata(legacy)
        legacy.create_all(self.engine)
        with self.engine.begin() as conn:
            conn.execute(legacy.tables['users'].insert(), [
                {'email': 'a@example.com', 'password_hash': 'unused'},
                {'email': 'b@example.test', 'password_hash': 'unused'},
            ])
            conn.exec_driver_sql(Path('migrations/google_sign_in.sql').read_text())

    def clean_schema(self):
        with self.admin.begin() as conn: conn.exec_driver_sql(f'DROP SCHEMA "{self.schema}" CASCADE')
        self.admin.dispose()

    def test_migration_is_nullable_unique_and_preserves_existing_users(self):
        with self.engine.connect() as conn:
            columns = {c['name']: c for c in inspect(conn).get_columns('users')}
            self.assertTrue(columns['google_sub']['nullable'])
            self.assertTrue(any(c['column_names'] == ['google_sub'] for c in inspect(conn).get_unique_constraints('users')))
        with self.sessions() as db:
            self.assertEqual(db.query(User).count(), 2)
            self.assertTrue(all(u.google_sub is None for u in db.query(User).all()))
            db.get(User, 1).google_sub = 'unique'; db.commit()
            db.get(User, 2).google_sub = 'unique'
            with self.assertRaises(IntegrityError): db.commit()
            db.rollback()

    def test_concurrent_first_sign_in_returns_one_local_user(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(lambda _: self.login(), range(2)))
        self.assertEqual([r.status_code for r in responses], [200, 200])
        with self.sessions() as db:
            self.assertEqual(db.query(User).filter(User.google_sub == 'google-123').count(), 1)
            self.assertEqual(db.query(User).count(), 3)
