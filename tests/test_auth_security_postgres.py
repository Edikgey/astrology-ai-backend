"""Real PostgreSQL atomic limits, reset races and additive migration."""
import json
import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4
from sqlalchemy import create_engine, MetaData, Table, inspect
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker
from fastapi import HTTPException
from starlette.requests import Request
import test_my_charts as fixtures
import test_auth_security as security_fixtures
from database.connection import Base
from database.queries import User, PasswordReset
from modules.security_limits import enforce
from modules.password_recovery import prepare_reset


@unittest.skipUnless(os.getenv('USAGE_TEST_POSTGRES_URL'), 'Local PostgreSQL test URL not configured')
class AuthSecurityPostgresTests(security_fixtures.AuthSecurityTests):
    def setUp(self):
        url=make_url(os.environ['USAGE_TEST_POSTGRES_URL'])
        if url.get_backend_name()!='postgresql' or url.host not in ('localhost','127.0.0.1','::1') or not url.database.startswith('test_'):
            raise RuntimeError('Only a local test database is allowed')
        super().setUp();self.engine.dispose()
        self.schema='auth_security_'+uuid4().hex
        self.admin=create_engine(url)
        with self.admin.begin() as conn: conn.exec_driver_sql(f'CREATE SCHEMA "{self.schema}"')
        self.addCleanup(self.cleanup_schema)
        self.engine=create_engine(url,connect_args={'options':f'-csearch_path={self.schema}'})
        self.sessions=sessionmaker(bind=self.engine,autoflush=False)
        legacy=MetaData()
        for table in Base.metadata.sorted_tables:
            if table.name in ('security_rate_limits','password_resets'):continue
            if table.name=='users':Table('users',legacy,*(c._copy() for c in table.columns if c.name!='auth_version'))
            else:table.to_metadata(legacy)
        legacy.create_all(self.engine)
        with self.engine.begin() as conn:
            conn.execute(legacy.tables['users'].insert(),[{'id':1,'email':'a@example.test','password_hash':'unused'},{'id':2,'email':'b@example.test','password_hash':'unused'}])
            conn.exec_driver_sql("SELECT setval(pg_get_serial_sequence('users','id'), 2, true)")
            self.before=[dict(row) for row in conn.execute(legacy.tables['users'].select()).mappings()]
            conn.exec_driver_sql(Path('migrations/auth_security.sql').read_text())

    def cleanup_schema(self):
        self.engine.dispose()
        with self.admin.begin() as conn:conn.exec_driver_sql(f'DROP SCHEMA "{self.schema}" CASCADE')
        self.admin.dispose()

    def test_migration_preserves_existing_data_and_default_legacy_sessions(self):
        with self.engine.connect() as conn:
            rows=[dict(row) for row in conn.exec_driver_sql('SELECT * FROM users ORDER BY id').mappings()]
            for before,after in zip(self.before,rows):
                self.assertEqual(before,{k:v for k,v in after.items() if k!='auth_version'})
                self.assertEqual(after['auth_version'],0)
            self.assertEqual(inspect(conn).get_foreign_keys('password_resets')[0]['options']['ondelete'],'CASCADE')
        self.assertEqual(self.client.get('/auth/me',headers=self.owner).status_code,200)

    def test_concurrent_rate_consumption_is_atomic_and_survives_new_sessions(self):
        request=Request({'type':'http','headers':[],'client':('198.51.100.1',123)})
        def attempt(_):
            with self.sessions() as db:
                try:enforce(db,request,'login',email='race@example.com');return 200
                except HTTPException as e:return e.status_code
        with ThreadPoolExecutor(max_workers=5) as workers:results=list(workers.map(attempt,range(16)))
        self.assertEqual(results.count(200),10);self.assertEqual(results.count(429),6)
        self.assertEqual(attempt(None),429)

    def test_two_simultaneous_resets_with_different_tokens_change_password_once(self):
        self.set_account()
        a=prepare_reset(self.engine,'owner@example.com')[1]
        b=prepare_reset(self.engine,'owner@example.com')[1]
        with ThreadPoolExecutor(max_workers=2) as workers:responses=list(workers.map(self.confirm,[a,b]))
        self.assertEqual(sorted(r.status_code for r in responses),[200,400])
        with self.sessions() as db:
            self.assertEqual(db.get(User,1).auth_version,1)
            self.assertEqual(db.query(PasswordReset).count(),0)


if __name__=='__main__':unittest.main()
