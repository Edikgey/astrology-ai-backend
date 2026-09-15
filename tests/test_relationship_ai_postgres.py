"""Shared account pool and real subject locking against the exact pending migration."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
import os
import unittest
from fastapi import HTTPException
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError
import test_relationships_postgres as postgres_fixtures
from test_synastry import facts
from database.queries import (User, NatalChart, ChartData, Relationship, GPTUsage,
                              RelationshipMessage, RelationshipConversation, GPTConversation)
from modules import usage
from modules.ai_conversation import AIAnswer, MemoryUpdate
from modules.synastry import build_synastry_snapshot
from api.relationships import delete_relationship
import test_my_charts as fixtures


@unittest.skipUnless(os.getenv('USAGE_TEST_POSTGRES_URL'), 'Local PostgreSQL test URL not configured')
class RelationshipAIPostgresTests(unittest.TestCase):
    setUp = postgres_fixtures.RelationshipPostgresTests.setUp
    cleanup_schema = postgres_fixtures.RelationshipPostgresTests.cleanup_schema
    snapshot = postgres_fixtures.RelationshipPostgresTests.snapshot
    apply = postgres_fixtures.RelationshipPostgresTests.apply
    attempt_create = postgres_fixtures.RelationshipPostgresTests.attempt_create

    def subjects(self):
        first = self.attempt_create().id
        with self.sessions() as db:
            info, stored = facts(3, 60)
            chart = NatalChart(id=3, user_id=1, birth_utc=info.birth_utc, **fixtures.PAYLOAD)
            data = ChartData(chart_id=3, bodies_for_circle=stored.bodies_for_circle, aspects_for_circle=[],
                             houses=stored.houses, house_system='Placidus')
            db.add(chart); db.flush(); db.add(data); db.flush()
            a = db.get(NatalChart, 1); a_data = db.query(ChartData).filter_by(chart_id=1).one()
            second = Relationship(user_id=1, chart_a_id=1, chart_b_id=3, person_a_label='A', person_b_label='C',
                ruleset_version='synastry-major-8-v1', calculation=build_synastry_snapshot(a, a_data, chart, data))
            db.add(second); db.flush(); second_id = second.id; db.commit()
        return first, second_id

    def test_migration_old_rows_valid_subject_xor_and_new_storage_constraints(self):
        self.assertEqual(self.before, self.snapshot())
        columns = {c['name']: c for c in inspect(self.engine).get_columns('gpt_usage')}
        self.assertTrue(columns['chart_id']['nullable'])
        self.assertTrue(columns['relationship_id']['nullable'])
        self.assertTrue(columns['source_relationship_message_id']['nullable'])
        rid, _ = self.subjects()
        with self.sessions() as db:
            old = db.query(GPTUsage).one()
            self.assertIsNone(old.relationship_id)
            for subject in ({}, {'chart_id': 1, 'relationship_id': rid}):
                db.add(GPTUsage(user_id=1, status='reserved', plan='free', **subject))
                with self.assertRaises(IntegrityError): db.commit()
                db.rollback()
            for fields in ({'chart_id': 1}, {'relationship_id': rid}):
                reservation = usage.reserve(db, 1, **fields)
                usage.finalize(db, 1, reservation, 'Question', 'Answer')
            self.assertEqual(usage.account_usage(db, 1)['gpt_messages_used'], 3)
            db.add(RelationshipConversation(relationship_id=rid, summary='memory', through_message_id=1)); db.commit()
            with self.assertRaises(IntegrityError):
                db.add(RelationshipConversation(relationship_id=rid, summary='duplicate', through_message_id=2)); db.commit()
            db.rollback()

    def race(self, fields, premium=False):
        with self.sessions() as db:
            now = usage.utcnow()
            if premium:
                usage.set_plan(db, 1, 'premium', now-timedelta(days=1), now+timedelta(days=28))
                user = db.get(User, 1)
                plan, count, start = 'premium', 299, user.current_period_start
            else:
                plan, count, start = 'free', 8, None  # one pre-migration success already exists
            db.add_all([GPTUsage(user_id=1, chart_id=1, status='succeeded', plan=plan, period_start=start) for _ in range(count)])
            db.commit()
        barrier = Barrier(2)
        def reserve(subject):
            with self.sessions() as db:
                barrier.wait(timeout=5)
                try:
                    result = usage.reserve(db, 1, **subject)
                    self.assertFalse(db.in_transaction())
                    return result
                except HTTPException as error:
                    self.assertEqual(error.detail['code'], 'GPT_LIMIT_REACHED')
                    self.assertFalse(db.in_transaction())
                    return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(reserve, fields))
        winner, = [item for item in outcomes if item]
        with self.sessions() as db:
            usage.finalize(db, 1, winner, 'Final slot', 'Answer')
            state = usage.account_usage(db, 1)
            self.assertEqual(state['gpt_messages_available'], 0)
            self.assertEqual(state['gpt_messages_used'], 300 if premium else 10)

    def test_natal_relationship_final_free_slot(self):
        rid, _ = self.subjects()
        self.race([{'chart_id': 1}, {'relationship_id': rid}])

    def test_two_relationships_final_free_slot(self):
        a, b = self.subjects()
        self.race([{'relationship_id': a}, {'relationship_id': b}])

    def test_natal_relationship_final_premium_slot(self):
        rid, _ = self.subjects()
        self.race([{'chart_id': 1}, {'relationship_id': rid}], premium=True)

    def test_same_relationship_busy_but_same_numeric_natal_id_independent(self):
        rid, _ = self.subjects(); self.assertEqual(rid, 1)
        with self.sessions() as db:
            a = usage.reserve(db, 1, 1)
            b = usage.reserve(db, 1, relationship_id=1)
            with self.assertRaises(HTTPException) as busy: usage.reserve(db, 1, relationship_id=1)
            self.assertEqual(busy.exception.detail['code'], 'GPT_REQUEST_IN_PROGRESS')
            usage.finalize(db, 1, a, 'Natal', AIAnswer('Natal', metadata={}, memory_update=MemoryUpdate(0, 1, 'NATAL')))
            usage.finalize(db, 1, b, 'Relationship', AIAnswer('Pair', metadata={}, memory_update=MemoryUpdate(0, 1, 'PAIR')))
            self.assertEqual(db.get(GPTConversation, 1).summary, 'NATAL')
            self.assertEqual(db.get(RelationshipConversation, 1).summary, 'PAIR')

    def test_concurrent_same_relationship_with_spare_quota(self):
        rid, _ = self.subjects(); barrier = Barrier(2)
        def reserve():
            with self.sessions() as db:
                barrier.wait(timeout=5)
                try: return usage.reserve(db, 1, relationship_id=rid)
                except HTTPException as error:
                    self.assertEqual(error.detail['code'], 'GPT_REQUEST_IN_PROGRESS'); return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = [f.result(timeout=15) for f in [pool.submit(reserve), pool.submit(reserve)]]
        self.assertEqual(sum(r is not None for r in results), 1)

    def test_delete_cascades_content_but_preserves_ledger_and_releases_late_answer(self):
        rid, _ = self.subjects()
        with self.sessions() as db:
            usage.finalize(db, 1, usage.reserve(db, 1, relationship_id=rid), 'Question',
                           AIAnswer('Pair', metadata={}, memory_update=MemoryUpdate(0, 1, 'PAIR')))
            pending = usage.reserve(db, 1, relationship_id=rid)
        with self.sessions() as db: delete_relationship(rid, db.get(User, 1), db)
        with self.sessions() as db:
            with self.assertRaises(HTTPException) as error: usage.finalize(db, 1, pending, 'Late', 'Late')
            self.assertEqual(error.exception.status_code, 404)
            usage.release(db, 1, pending)
            self.assertEqual(db.query(RelationshipMessage).count(), 0)
            self.assertEqual(db.query(RelationshipConversation).count(), 0)
            self.assertEqual(db.get(GPTUsage, pending).status, 'released')
            success = db.query(GPTUsage).filter_by(relationship_id=rid, status='succeeded').one()
            self.assertIsNone(success.chart_id); self.assertIsNone(success.source_relationship_message_id)
            self.assertEqual(usage.account_usage(db, 1)['gpt_messages_used'], 2)

    def test_concurrent_delete_finalize_serialize_without_orphans(self):
        rid, _ = self.subjects()
        with self.sessions() as db: pending = usage.reserve(db, 1, relationship_id=rid)
        barrier = Barrier(2)
        def finalize():
            with self.sessions() as db:
                barrier.wait(timeout=5)
                try:
                    usage.finalize(db, 1, pending, 'Pair', 'Answer'); return 'succeeded'
                except HTTPException as error:
                    self.assertEqual(error.status_code, 404)
                    usage.release(db, 1, pending); return 'released'
        def delete():
            with self.sessions() as db:
                user = db.get(User, 1); barrier.wait(timeout=5)
                delete_relationship(rid, user, db)
        with ThreadPoolExecutor(max_workers=2) as pool:
            a, b = pool.submit(finalize), pool.submit(delete)
            outcome = a.result(timeout=15); b.result(timeout=15)
        with self.sessions() as db:
            self.assertIsNone(db.get(Relationship, rid))
            self.assertEqual(db.query(RelationshipMessage).count(), 0)
            self.assertEqual(db.get(GPTUsage, pending).status, outcome)

    def test_expiry_prevents_stale_memory_after_new_answer(self):
        rid, _ = self.subjects()
        with self.sessions() as db:
            old = usage.reserve(db, 1, relationship_id=rid)
            db.get(GPTUsage, old).expires_at = usage.utcnow()-timedelta(seconds=1); db.commit()
            new = usage.reserve(db, 1, relationship_id=rid)
            usage.finalize(db, 1, new, 'New', AIAnswer('Answer', metadata={}, memory_update=MemoryUpdate(0, 1, 'NEW')))
            with self.assertRaises(HTTPException):
                usage.finalize(db, 1, old, 'Old', AIAnswer('Late', metadata={}, memory_update=MemoryUpdate(0, 1, 'OLD')))
            self.assertEqual(db.get(RelationshipConversation, rid).summary, 'NEW')
            self.assertEqual(db.query(RelationshipMessage).count(), 2)
