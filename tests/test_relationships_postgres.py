"""Exact migration and real locks; only the established disposable local PG setup."""
import os
from pathlib import Path
from uuid import uuid4
from threading import Barrier
from concurrent.futures import ThreadPoolExecutor
import unittest
from sqlalchemy import create_engine, text, inspect
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker
from sqlalchemy.exc import IntegrityError, ProgrammingError
from fastapi import Response, HTTPException
import test_my_charts as fixtures
from test_synastry import facts
from database.queries import User, NatalChart, ChartData, GPTMessage, GPTUsage, Relationship
from api.relationships import create_relationship
from api.endpoints import delete_natal_chart
from models.relationship import RelationshipCreate


@unittest.skipUnless(os.getenv('USAGE_TEST_POSTGRES_URL'), 'Local PostgreSQL test URL not configured')
class RelationshipPostgresTests(unittest.TestCase):
    def setUp(self):
        url = make_url(os.environ['USAGE_TEST_POSTGRES_URL'])
        if (url.get_backend_name() != 'postgresql' or url.host not in ('localhost', '127.0.0.1', '::1') or
                not ((url.database or '').startswith('test_') or (url.database or '').endswith('_test'))):
            raise RuntimeError('Only a named local test database is allowed')
        self.schema = 'relationship_test_' + uuid4().hex
        self.admin = create_engine(url)
        with self.admin.begin() as db:
            db.exec_driver_sql(f'CREATE SCHEMA "{self.schema}"')
        self.engine = create_engine(url, connect_args={'options': f'-csearch_path={self.schema}'})
        self.addCleanup(self.cleanup_schema)
        self.tables = [t for t in fixtures.Base.metadata.sorted_tables if t.name != 'relationships']
        fixtures.Base.metadata.create_all(self.engine, tables=self.tables)
        self.sessions = sessionmaker(self.engine, autoflush=False)
        with self.sessions() as db:
            db.add_all([User(id=1, email='one@example.test', password_hash='unused'),
                        User(id=2, email='two@example.test', password_hash='unused')])
            db.flush()
            for identifier in (1, 2):
                chart, stored = facts(identifier, (identifier - 1) * 30)
                db.add(NatalChart(id=identifier, user_id=1, birth_utc=chart.birth_utc, **fixtures.PAYLOAD))
                db.flush()
                db.add(ChartData(chart_id=identifier, bodies_for_circle=stored.bodies_for_circle,
                                 aspects_for_circle=[], houses=stored.houses, house_system='Placidus'))
                db.add(GPTMessage(chart_id=identifier, role='gpt', content='Existing history'))
            db.add(GPTUsage(user_id=1, chart_id=1, status='succeeded', plan='free'))
            db.commit()
        self.script = (Path(__file__).parents[1] / 'migrations/relationships.sql').read_text(encoding='utf-8')
        self.before = self.snapshot()
        self.apply(self.script)

    def cleanup_schema(self):
        self.engine.dispose()
        with self.admin.begin() as db:
            db.exec_driver_sql(f'DROP SCHEMA "{self.schema}" CASCADE')
        self.admin.dispose()

    def snapshot(self):
        with self.engine.connect() as db:
            return {t.name: db.execute(t.select().order_by(*t.primary_key.columns)).all() for t in self.tables}

    def apply(self, script):
        with self.engine.connect() as db:
            try:
                db.exec_driver_sql(script)
                db.commit()
            except BaseException:
                db.rollback()
                raise

    def attempt_create(self, reverse=False):
        with self.sessions() as db:
            response = Response()
            result = create_relationship(RelationshipCreate(chart_a_id=2 if reverse else 1,
                chart_b_id=1 if reverse else 2, person_a_label='B' if reverse else 'A',
                person_b_label='A' if reverse else 'B'), response, db.get(User, 1), db)
            self.assertFalse(db.in_transaction())
            return result

    def test_exact_migration_preserves_rows_and_matches_orm(self):
        self.assertEqual(self.before, self.snapshot())
        inspector = inspect(self.engine)
        columns = {c['name']: c for c in inspector.get_columns('relationships')}
        self.assertEqual(set(columns), set(Relationship.__table__.columns.keys()))
        self.assertEqual(str(columns['calculation']['type']), 'JSONB')
        for column in Relationship.__table__.columns:
            self.assertEqual(columns[column.name]['nullable'], column.nullable)
        indexes = inspector.get_indexes('relationships')
        self.assertEqual({i['name'] for i in indexes}, {i.name for i in Relationship.__table__.indexes})
        self.assertTrue(next(i for i in indexes if i['name'] == 'uq_relationships_owner_pair')['unique'])
        self.assertEqual({c['name'] for c in inspector.get_check_constraints('relationships')},
                         {c.name for c in Relationship.__table__.constraints if c.__class__.__name__ == 'CheckConstraint'})
        chart_fks = [fk for fk in inspector.get_foreign_keys('relationships') if fk['referred_table'] == 'natal_charts']
        self.assertEqual(len(chart_fks), 2)
        self.assertTrue(all(fk['options']['ondelete'] == 'RESTRICT' for fk in chart_fks))
        self.attempt_create()

    def test_repeat_migration_fails_without_data_changes(self):
        created = self.attempt_create()
        with self.assertRaises(ProgrammingError):
            self.apply(self.script)
        self.assertEqual(self.before, self.snapshot())
        with self.sessions() as db:
            self.assertEqual(db.get(Relationship, created.id).person_a_label, 'A')

    def test_failed_migration_rolls_back_all_new_objects(self):
        with self.engine.begin() as db:
            db.exec_driver_sql('DROP TABLE relationships')
        with self.assertRaises(ProgrammingError):
            self.apply(self.script.replace('COMMIT;', 'SELECT missing_relationship_test_column;\nCOMMIT;'))
        self.assertFalse(inspect(self.engine).has_table('relationships'))
        self.assertEqual(self.before, self.snapshot())
        self.apply(self.script)

    def test_database_blocks_reverse_duplicate_and_chart_delete(self):
        self.attempt_create()
        with self.sessions() as db:
            db.add(Relationship(user_id=1, chart_a_id=2, chart_b_id=1, person_a_label='B', person_b_label='A',
                                ruleset_version='test', calculation={}))
            with self.assertRaises(IntegrityError):
                db.commit()
            db.rollback()
            with self.assertRaises(IntegrityError) as deletion:
                db.execute(text('DELETE FROM chart_data WHERE chart_id=1'))
                db.execute(text('DELETE FROM gpt_messages WHERE chart_id=1'))
                db.execute(text('DELETE FROM natal_charts WHERE id=1'))
            self.assertEqual(deletion.exception.orig.diag.constraint_name, 'relationships_chart_a_id_fkey')
            db.rollback()
            db.query(Relationship).delete()
            db.commit()
        self.assertEqual(self.before, self.snapshot())

    def test_concurrent_reverse_post_returns_one_relationship(self):
        barrier = Barrier(2)
        def attempt(reverse):
            barrier.wait(timeout=5)
            return self.attempt_create(reverse)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(attempt, (False, True)))
        self.assertEqual(results[0], results[1])
        with self.sessions() as db:
            self.assertEqual(db.query(Relationship).count(), 1)
            self.assertEqual(db.query(GPTUsage).count(), 1)

    def test_create_versus_natal_delete_is_atomic(self):
        barrier = Barrier(2)
        def create():
            barrier.wait(timeout=5)
            try:
                self.attempt_create()
                return 'created'
            except HTTPException as error:
                self.assertEqual(error.status_code, 404)
                return 'missing'
        def delete():
            with self.sessions() as db:
                user = db.get(User, 1)
                barrier.wait(timeout=5)
                try:
                    delete_natal_chart(1, user, db)
                    return 'deleted'
                except HTTPException as error:
                    self.assertEqual(error.status_code, 409)
                    self.assertEqual(error.detail['code'], 'RELATIONSHIP_DEPENDENCIES_EXIST')
                    self.assertFalse(db.in_transaction())
                    return 'blocked'
        with ThreadPoolExecutor(max_workers=2) as pool:
            a, b = pool.submit(create), pool.submit(delete)
            outcome = (a.result(timeout=15), b.result(timeout=15))
        self.assertIn(outcome, [('created', 'blocked'), ('missing', 'deleted')])
        with self.sessions() as db:
            self.assertEqual(db.query(Relationship).count(), int(outcome[0] == 'created'))
            self.assertEqual(db.query(NatalChart).count(), 2 if outcome[0] == 'created' else 1)
            self.assertEqual(db.query(GPTUsage).count(), 1)
