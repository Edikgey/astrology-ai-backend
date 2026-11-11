"""Auth/reset/guest protection, isolated DB + mocked mail (no production calls)."""
import hashlib
import os
import unittest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch
from starlette.requests import Request
import test_my_charts as fixtures
from api.auth import create_access_token, get_password_hash
from database.queries import User, PasswordReset, SecurityRateLimit, EmailVerificationCode
from modules import password_recovery, security_limits
from services.email_service import EmailDeliveryError

PASSWORD = 'New-password9!'


class AuthSecurityTests(unittest.TestCase):
    setUp = fixtures.MyChartsTests.setUp
    tearDown = fixtures.MyChartsTests.tearDown

    def set_account(self):
        with self.sessions() as db:
            user = db.get(User, 1); user.email = 'owner@example.com'; user.password_hash = get_password_hash('old')
            db.commit()

    def request_reset(self, email='owner@example.com'):
        return self.client.post('/auth/request-password-reset', json={'email': email})

    def token(self):
        self.set_account()
        with patch('modules.password_recovery.send_password_reset_email', new_callable=AsyncMock) as mail:
            response = self.request_reset()
            self.assertEqual(response.status_code, 200, response.text)
            return mail.call_args.args[1]

    def confirm(self, token, **changes):
        return self.client.post('/auth/reset-password', json={
            'token': token, 'password': PASSWORD, 'password_confirmation': PASSWORD, **changes})

    def test_reset_is_one_use_changes_hash_revokes_old_tokens_preserves_other_users(self):
        token = self.token()
        with self.sessions() as db:
            row = db.query(PasswordReset).one()
            self.assertEqual(row.token_hash, hashlib.sha256(token.encode()).hexdigest())
            self.assertNotEqual(row.token_hash, token)
            other_hash = db.get(User, 2).password_hash
        old = create_access_token({'sub': '1'})
        response = self.confirm(token)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers['cache-control'], 'no-store')
        self.assertNotIn(token, response.text); self.assertNotIn(PASSWORD, response.text)
        self.assertEqual(self.confirm(token).status_code, 400)
        self.assertEqual(self.client.get('/auth/me', headers={'Authorization': 'Bearer '+old}).status_code, 401)
        login = lambda password: self.client.post('/auth/login', json={'email':'owner@example.com','password':password})
        self.assertEqual(login('old').status_code, 401)
        new = login(PASSWORD); self.assertEqual(new.status_code, 200, new.text)
        self.assertEqual(self.client.get('/auth/me', headers={'Authorization': 'Bearer '+new.json()['access_token']}).status_code, 200)
        self.assertEqual(self.client.get('/auth/me', headers=self.other).status_code, 200)
        with self.sessions() as db:
            self.assertEqual(db.get(User, 1).auth_version, 1)
            self.assertEqual(db.get(User, 2).password_hash, other_hash)
            self.assertEqual(db.query(PasswordReset).count(), 0)

    def test_generic_response_for_existing_unknown_and_ambiguous_email(self):
        self.set_account()
        with patch('modules.password_recovery.send_password_reset_email', new_callable=AsyncMock) as mail:
            known = self.request_reset()
            missing = self.request_reset('missing@example.com')
            self.assertEqual(known.status_code, missing.status_code)
            self.assertEqual(known.json(), missing.json())
            self.assertEqual(mail.await_count, 1)
            with self.sessions() as db:
                db.add(User(id=3, email='OWNER@example.com', password_hash='unchanged')); db.commit()
            with patch('modules.security_limits.server_now', return_value=datetime.utcnow()+timedelta(seconds=61)):
                ambiguous = self.request_reset()
            self.assertEqual(ambiguous.json(), known.json()); self.assertEqual(mail.await_count, 1)

    def test_expired_and_invalid_tokens_leave_password_unchanged(self):
        token = self.token()
        with self.sessions() as db:
            before = db.get(User, 1).password_hash
            db.query(PasswordReset).update({'expires_at':datetime.utcnow()-timedelta(seconds=1)}); db.commit()
        for candidate in (token, 'x'*43):
            response = self.confirm(candidate); self.assertEqual(response.status_code, 400)
            self.assertNotIn(candidate, response.text)
        with self.sessions() as db: self.assertEqual(db.get(User,1).password_hash, before)

    def test_new_policy_confirmation_and_redaction_apply_to_reset(self):
        token = self.token()
        from test_password_policy import INVALID, VALID
        for password in INVALID:
            response = self.confirm(token, password=password, password_confirmation=password)
            self.assertEqual(response.status_code, 422)
            self.assertNotIn(token, response.text)
            self.assertTrue(all('input' not in error for error in response.json()['detail']))
        self.assertEqual(self.confirm(token, password_confirmation='mismatch').status_code, 422)
        self.assertEqual(self.client.post('/auth/reset-password',json={'token':token,'password':PASSWORD}).status_code,422)
        self.assertEqual(self.confirm(token,password=VALID[1],password_confirmation=VALID[1]).status_code,200)

    def test_mail_failure_is_generic_and_invalidates_only_failed_link(self):
        self.set_account()
        with patch('modules.password_recovery.send_password_reset_email', new_callable=AsyncMock, side_effect=EmailDeliveryError('private')) as mail:
            response = self.request_reset()
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('private', response.text)
        with self.sessions() as db:
            self.assertEqual(db.query(PasswordReset).count(), 0)
            self.assertEqual(db.get(User,1).auth_version, 0)

    def test_request_rate_limit_does_not_leak_account_and_expires(self):
        with patch('modules.password_recovery.send_password_reset_email',new_callable=AsyncMock):
            self.set_account()
            self.assertEqual(self.request_reset().status_code, 200)
            blocked = self.request_reset()
            self.assertEqual(blocked.status_code, 429); self.assertGreater(int(blocked.headers['retry-after']),0)
            with patch('modules.security_limits.server_now', return_value=datetime.utcnow()+timedelta(seconds=61)):
                self.assertEqual(self.request_reset().status_code, 200)

    def test_login_blocks_repeated_attempts_without_changing_password(self):
        self.set_account()
        for _ in range(10):
            response = self.client.post('/auth/login',json={'email':'owner@example.com','password':'wrong'})
            self.assertEqual(response.status_code, 401)
        blocked = self.client.post('/auth/login',json={'email':'owner@example.com','password':'old'})
        self.assertEqual(blocked.status_code, 429)
        cors = self.client.post('/auth/login',json={'email':'owner@example.com','password':'old'},headers={'Origin':'https://mylunariaai.com'})
        self.assertEqual(cors.status_code,429)
        self.assertEqual(cors.headers['access-control-allow-origin'],'https://mylunariaai.com')
        self.assertIn('Retry-After',cors.headers['access-control-expose-headers'])
        with patch('modules.security_limits.server_now',return_value=datetime.utcnow()+timedelta(minutes=16)):
            self.assertEqual(self.client.post('/auth/login',json={'email':'owner@example.com','password':'old'}).status_code,200)

    def test_verification_body_and_attempt_limits_preserve_code(self):
        with patch('api.auth.send_confirmation_email',new_callable=AsyncMock):
            body={'email':'new@example.com','password':PASSWORD}
            self.assertEqual(self.client.post('/auth/request-register',json=body).status_code,200)
            for _ in range(10):
                self.assertEqual(self.client.post('/auth/verify-code',json={**body,'code':'000000'}).status_code,400)
            with self.sessions() as db: code=db.query(EmailVerificationCode).one().code
            self.assertEqual(self.client.post('/auth/verify-code',json={**body,'code':code}).status_code,429)
            with self.sessions() as db: self.assertFalse(db.query(EmailVerificationCode).one().used)

    def test_guest_limits_cannot_be_bypassed_by_changing_token(self):
        with patch('api.endpoints._create_natal_chart',return_value={'ignored':True}) as calculate:
            # Call the handler with a valid identity to isolate protection from response schema.
            from api.endpoints import create_natal_chart
            from models.natal_chart import NatalChartCreate
            from uuid import uuid4
            request=Request({'type':'http','headers':[],'client':('198.51.100.1',123)})
            for i in range(10):
                with self.sessions() as db:
                    create_natal_chart(NatalChartCreate(**fixtures.PAYLOAD),request,{'user':None,'session_token':uuid4()},db)
            from fastapi import HTTPException
            with self.sessions() as db, self.assertRaises(HTTPException) as caught:
                create_natal_chart(NatalChartCreate(**fixtures.PAYLOAD),request,{'user':None,'session_token':uuid4()},db)
            self.assertEqual(caught.exception.status_code,429);self.assertEqual(calculate.call_count,10)

    def test_limit_rows_contain_only_hashed_identifiers(self):
        self.client.post('/auth/login',json={'email':'private@example.com','password':'wrong'})
        with self.sessions() as db:
            rows=db.query(SecurityRateLimit).all();self.assertTrue(rows)
            self.assertTrue(all(len(r.bucket_key)==64 and 'private' not in r.bucket_key for r in rows))

    def test_forwarded_headers_are_ignored_outside_railway(self):
        request=Request({'type':'http','headers':[(b'x-real-ip',b'203.0.113.2'),(b'x-forwarded-for',b'203.0.113.3')],'client':('198.51.100.1',123)})
        with patch.dict(os.environ,{'RAILWAY_ENVIRONMENT_ID':''}):
            self.assertEqual(security_limits.client_address(request),'198.51.100.1')
        with patch.dict(os.environ,{'RAILWAY_ENVIRONMENT_ID':'test'}):
            self.assertEqual(security_limits.client_address(request),'203.0.113.2')

    def test_large_chunked_auth_body_rejected_without_email_or_secret_echo(self):
        with patch('api.auth.send_confirmation_email',new_callable=AsyncMock) as mail:
            response=self.client.post('/auth/request-register',content=iter([b'{' + b'x'*20000,b'x'*20000]),headers={'Origin':'https://mylunariaai.com'})
        self.assertEqual(response.status_code,413);self.assertEqual(response.headers['access-control-allow-origin'],'https://mylunariaai.com');mail.assert_not_awaited()

    def test_reset_email_uses_existing_smtp_configuration_and_trusted_fragment_link(self):
        from test_email_auth import ENV
        from services.email_service import send_password_reset_email
        import asyncio
        token='t'*43
        with patch.dict(os.environ,ENV),patch('services.email_service.FastMail') as factory:
            mail=factory.return_value.send_message=AsyncMock()
            asyncio.run(send_password_reset_email('owner@example.com',token))
            config=factory.call_args.args[0]
            self.assertEqual(config.MAIL_SERVER,ENV['MAIL_SERVER']);self.assertTrue(config.MAIL_STARTTLS and config.VALIDATE_CERTS)
            message=mail.call_args.args[0]
            self.assertIn('https://mylunariaai.com/reset-password#token='+token,message.body)
            self.assertIn('30 минут',message.body)

    def test_resend_failure_keeps_an_earlier_valid_reset_link(self):
        token=self.token()
        with patch('modules.security_limits.server_now',return_value=datetime.utcnow()+timedelta(seconds=61)),patch('modules.password_recovery.send_password_reset_email',new_callable=AsyncMock,side_effect=EmailDeliveryError()):
            self.assertEqual(self.request_reset().status_code,200)
        self.assertEqual(self.confirm(token).status_code,200)

    def test_registration_resend_is_throttled_without_replacing_current_code(self):
        with patch('api.auth.send_confirmation_email',new_callable=AsyncMock) as mail:
            body={'email':'new@example.com','password':PASSWORD}
            self.assertEqual(self.client.post('/auth/request-register',json=body).status_code,200)
            with self.sessions() as db:code=db.query(EmailVerificationCode).one().code
            self.assertEqual(self.client.post('/auth/request-register',json=body).status_code,429)
            self.assertEqual(mail.await_count,1)
            response=self.client.post('/auth/verify-code',json={**body,'code':code})
            self.assertEqual(response.status_code,200,response.text)

    def test_google_requests_are_limited_before_provider_verification(self):
        from fastapi import HTTPException
        with patch('api.auth.verify_google_credential',side_effect=HTTPException(401,'Неверный токен')) as verify:
            body={'credential':'invalid','nonce':'a'*64}
            for _ in range(30):
                self.assertEqual(self.client.post('/auth/google',json=body,headers={'Origin':'https://mylunariaai.com'}).status_code,401)
            self.assertEqual(self.client.post('/auth/google',json=body,headers={'Origin':'https://mylunariaai.com'}).status_code,429)
            self.assertEqual(verify.call_count,30)

    def test_reset_preserves_premium_and_google_signin_issues_current_session(self):
        token=self.token();end=datetime.utcnow()+timedelta(days=30)
        with self.sessions() as db:
            user=db.get(User,1);user.plan='premium';user.current_period_end=end;db.commit()
        self.assertEqual(self.confirm(token).status_code,200)
        with self.sessions() as db:
            user=db.get(User,1);self.assertEqual(user.plan,'premium');self.assertEqual(user.current_period_end,end)
        with patch('api.auth.verify_google_credential',return_value={'sub':'google-owner','email':'owner@example.com','authoritative':True}):
            response=self.client.post('/auth/google',json={'credential':'mock','nonce':'a'*64},headers={'Origin':'https://mylunariaai.com'})
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(self.client.get('/auth/me',headers={'Authorization':'Bearer '+response.json()['access_token']}).status_code,200)


if __name__ == '__main__': unittest.main()
