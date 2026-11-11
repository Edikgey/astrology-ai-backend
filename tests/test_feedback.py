import unittest
from datetime import datetime,timedelta
from uuid import uuid4
from unittest.mock import patch
import test_my_charts as fixtures
from database.queries import Feedback,User


class FeedbackTests(unittest.TestCase):
    setUp=fixtures.MyChartsTests.setUp
    tearDown=fixtures.MyChartsTests.tearDown

    def send(self,**kwargs):
        return self.client.post('/feedback',headers=self.owner,json={
            'request_id':str(uuid4()),'category':'idea','message':'Новая идея',**kwargs})

    def test_auth_and_no_public_inbox(self):
        for headers in ({},self.guest,{'Authorization':'Bearer invalid'}):
            for path in ('/feedback','/feedback/limits'):
                method=self.client.post if path=='/feedback' else self.client.get
                self.assertEqual(method(path,headers=headers).status_code,401)
        self.assertEqual(self.client.get('/feedback',headers=self.owner).status_code,405)

    def test_categories_identity_and_sanitized_path(self):
        for category in ('idea','bug','feedback'):
            r=self.send(category=category,message='  Спасибо '+category+'  ',page_path='/natal-chart-result/77?token=private#x')
            self.assertEqual(r.status_code,200,r.text)
            self.assertNotIn('Спасибо',r.text)
        with self.sessions() as db:
            rows=db.query(Feedback).all();self.assertEqual(len(rows),3)
            self.assertTrue(all(r.user_id==1 and r.page_path=='/natal-chart-result/:id' for r in rows))
            self.assertTrue(all(r.message==r.message.strip() for r in rows))
        self.assertEqual(self.send(user_id=2).status_code,422)
        self.assertEqual(self.client.get('/feedback/limits',headers=self.other).json()['remaining'],3)

    def test_invalid_messages_and_categories(self):
        for value in ('','  \n\t','\u200b','!!!','a'*2001,{},['text'],None,'text\x00'):
            with self.subTest(value_type=type(value).__name__): self.assertEqual(self.send(message=value).status_code,422)
        for value in ('other','IDEA',None):self.assertEqual(self.send(category=value).status_code,422)
        self.assertEqual(self.send(page_path='https://example.com/private').status_code,422)
        self.assertEqual(self.send(attachments=['file']).status_code,422)

    def test_exact_boundary_unicode_and_trim(self):
        for value in ('a'*2000,'я'*2000,'a'+'🌙'*1999):
            self.assertEqual(self.send(message=value).status_code,200)

    def test_limit_fourth_rejected_and_retry_idempotency(self):
        key=str(uuid4())
        self.assertEqual(self.send(request_id=key,message='one').status_code,200)
        self.assertTrue(self.send(request_id=key,message='one').json()['duplicate'])
        self.assertTrue(self.send(message='one').json()['duplicate'])
        self.assertEqual(self.send(request_id=key,message='changed').status_code,409)
        for value in ('two','three'):self.assertEqual(self.send(message=value).status_code,200)
        fourth=self.send(message='four');self.assertEqual(fourth.status_code,429)
        self.assertGreater(int(fourth.headers['retry-after']),0)
        self.assertEqual(fourth.json()['detail']['remaining'],0)
        self.assertTrue(self.send(request_id=key,message='one').json()['duplicate'])
        self.assertEqual(self.client.get('/feedback/limits',headers=self.owner).json()['remaining'],0)
        with self.sessions() as db:self.assertEqual(db.query(Feedback).count(),3)

    def test_rolling_server_time_boundary(self):
        now=datetime(2026,10,7,12)
        with patch('modules.feedback.server_now',return_value=now):
            for value in ('one','two','three'):self.send(message=value)
        with patch('modules.feedback.server_now',return_value=now+timedelta(hours=24)-timedelta(microseconds=1)):
            self.assertEqual(self.send(message='four').status_code,429)
        with patch('modules.feedback.server_now',return_value=now+timedelta(hours=24)):
            self.assertEqual(self.send(message='four').json()['remaining'],2)

    def test_size_content_type_and_malformed_json(self):
        self.assertEqual(self.client.post('/feedback',headers=self.owner,files={'file':('a.txt',b'text')}).status_code,415)
        h={**self.owner,'Content-Type':'application/json'}
        self.assertEqual(self.client.post('/feedback',headers=h,content='x'*16385).status_code,413)
        self.assertEqual(self.client.post('/feedback',headers=h,content='{broken').status_code,422)
        self.assertEqual(self.client.post('/feedback',headers=h,content=iter([b'x'*9000,b'x'*9000])).status_code,413)
        h['Content-Encoding']='gzip'
        self.assertEqual(self.client.post('/feedback',headers=h,content='{}').status_code,415)

    def test_storage_failure_does_not_expose_or_log_content(self):
        with patch('modules.feedback.submit',side_effect=RuntimeError('private feedback text')):
            with self.assertLogs('api.feedback',level='WARNING') as logs:r=self.send()
        self.assertEqual(r.status_code,503)
        self.assertNotIn('private feedback text',r.text+str(logs.output))

    def test_user_deletion_cascades_feedback(self):
        self.send()
        with self.sessions() as db:
            db.delete(db.get(User,1));db.commit()
            self.assertEqual(db.query(Feedback).count(),0)
