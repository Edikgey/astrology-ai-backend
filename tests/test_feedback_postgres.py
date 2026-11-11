import os,unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4
from pathlib import Path
from fastapi import HTTPException
import test_postgres_usage as pg
from api.feedback import FeedbackBody
from database.queries import Feedback,User
from modules import feedback


@unittest.skipUnless(os.getenv('USAGE_TEST_POSTGRES_URL'),'Local PostgreSQL required')
class FeedbackPostgresTests(unittest.TestCase):
    cleanup_schema=pg.PostgreSQLUsageTests.cleanup_schema
    snapshot=pg.PostgreSQLUsageTests.snapshot
    apply_script=pg.PostgreSQLUsageTests.apply_script

    def setUp(self):
        pg.PostgreSQLUsageTests.setUp(self)
        self.feedback_sql=(Path(__file__).parents[1]/'migrations/feedback.sql').read_text()
        self.apply_script(self.feedback_sql)

    def race(self,duplicate=False):
        barrier=Barrier(8);key=uuid4()
        def send(i):
            with self.sessions() as db:
                barrier.wait(timeout=10)
                body=FeedbackBody(request_id=key if duplicate else uuid4(),category='idea',message='Idea '+('same' if duplicate else str(i)))
                try:return feedback.submit(db,1,body)
                except HTTPException as exc:return exc.status_code
        with ThreadPoolExecutor(max_workers=8) as pool:return list(pool.map(send,range(8)))

    def test_concurrent_requests_cannot_exceed_three(self):
        rows=self.race()
        self.assertEqual(sum(x==429 for x in rows),5)
        with self.sessions() as db:self.assertEqual(db.query(Feedback).count(),3)

    def test_concurrent_retries_create_one_record(self):
        rows=self.race(True)
        self.assertEqual(sum(not x['duplicate'] for x in rows),1)
        with self.sessions() as db:self.assertEqual(db.query(Feedback).count(),1)

    def test_migration_preserves_data_and_foreign_key_deletion(self):
        self.assertEqual(self.snapshot(),self.before)
        with self.assertRaises(Exception):self.apply_script(self.feedback_sql)
        with self.sessions() as db:
            db.add(User(id=99,email='feedback@example.com',password_hash='test'));db.commit()
            feedback.submit(db,99,FeedbackBody(request_id=uuid4(),category='feedback',message='Test message'))
            db.delete(db.get(User,99));db.commit()
            self.assertEqual(db.query(Feedback).count(),0)
        self.assertEqual(self.snapshot(),self.before)
