"""Registration with the real email adapter and mocked SMTP; no external mail."""
import os
import unittest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

import test_my_charts as fixtures
from database.queries import EmailVerificationCode, User

ENV = {
    'MAIL_SERVER': 'smtp-relay.brevo.com', 'MAIL_PORT': '587',
    'MAIL_USERNAME': 'test@smtp-brevo.com', 'MAIL_PASSWORD': 'fake-smtp-key',
    'MAIL_FROM': 'noreply@mylunariaai.com', 'MAIL_FROM_NAME': 'Lunaria',
    'DEV_MODE': 'true',  # Must never silently suppress production registration mail.
}
BODY = {'email': 'registration@example.com', 'password': 'test-password-only'}
ORIGIN = 'https://mylunariaai.com'


class EmailAuthTests(unittest.TestCase):
    def setUp(self):
        fixtures.MyChartsTests.setUp(self)
        self.env = patch.dict(os.environ, ENV)
        self.mail = patch('services.email_service.FastMail')
        self.env.start()
        self.factory = self.mail.start()
        self.send = self.factory.return_value.send_message = AsyncMock()

    def tearDown(self):
        self.mail.stop()
        self.env.stop()
        fixtures.MyChartsTests.tearDown(self)

    def register(self):
        return self.client.post('/auth/request-register', json=BODY, headers={'Origin': ORIGIN})

    def verify(self, code):
        return self.client.post('/auth/verify-code', params={'code': code}, json=BODY)

    def code(self):
        with self.sessions() as db:
            return db.query(EmailVerificationCode).filter_by(email=BODY['email']).one().code

    def test_registration_brevo_verification_and_password_login(self):
        response = self.register()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers['access-control-allow-origin'], ORIGIN)
        conf = self.factory.call_args.args[0]
        self.assertEqual((conf.MAIL_SERVER, conf.MAIL_PORT), ('smtp-relay.brevo.com', 587))
        self.assertEqual((str(conf.MAIL_FROM), conf.MAIL_FROM_NAME), ('noreply@mylunariaai.com', 'Lunaria'))
        self.assertTrue(conf.MAIL_STARTTLS and conf.VALIDATE_CERTS and conf.USE_CREDENTIALS)
        self.assertFalse(conf.MAIL_SSL_TLS)
        self.assertEqual((conf.MAIL_DEBUG, conf.SUPPRESS_SEND), (0, 0))
        message = self.send.call_args.args[0]
        code = self.code()
        self.assertRegex(code, r'^\d{6}$')
        self.assertIn(code, message.body)
        self.assertNotIn(code, response.text)
        self.assertEqual([str(v) for v in message.recipients], [BODY['email']])
        with self.sessions() as db:
            self.assertIsNone(db.query(User).filter_by(email=BODY['email']).first())
        self.assertEqual(self.client.post('/auth/login', json=BODY).status_code, 401)
        verified = self.verify(code)
        self.assertEqual(verified.status_code, 200, verified.text)
        token = verified.json()['access_token']
        self.assertEqual(self.client.get('/auth/me', headers={'Authorization': 'Bearer '+token}).status_code, 200)
        self.assertEqual(self.client.post('/auth/login', json=BODY).status_code, 200)
        self.assertEqual(self.verify(code).status_code, 400)

    def test_smtp_failure_is_sanitized_and_does_not_leave_usable_code(self):
        self.send.side_effect = TimeoutError('fake-smtp-key private provider reply')
        with self.assertLogs('services.email_service', level='WARNING') as logs:
            response = self.register()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.headers['access-control-allow-origin'], ORIGIN)
        self.assertNotIn('fake-smtp-key', response.text + str(logs.output))
        self.assertNotIn('private provider reply', str(logs.output))
        with self.sessions() as db:
            self.assertIsNone(db.query(EmailVerificationCode).filter_by(email=BODY['email']).first())
            self.assertIsNone(db.query(User).filter_by(email=BODY['email']).first())
        self.send.side_effect = None
        self.assertEqual(self.register().status_code, 200)

    def test_missing_smtp_credentials_fails_closed_without_dev_bypass(self):
        with patch.dict(os.environ, {'MAIL_PASSWORD': ''}):
            self.assertEqual(self.register().status_code, 503)
        self.send.assert_not_awaited()

    def test_retry_replaces_previous_code_and_wrong_or_expired_code_is_rejected(self):
        with patch('api.auth.secrets.randbelow', side_effect=[123456, 654321]):
            self.assertEqual(self.register().status_code, 200)
            old = self.code()
            self.assertEqual(self.register().status_code, 200)
        current = self.code()
        self.assertNotEqual(old, current)
        self.assertEqual(self.verify(old).status_code, 400)
        with self.sessions() as db:
            record = db.query(EmailVerificationCode).filter_by(email=BODY['email']).one()
            record.expires_at = datetime.utcnow() - timedelta(seconds=1)
            db.commit()
        self.assertEqual(self.verify(current).status_code, 400)

    def test_failed_delivery_does_not_delete_newer_registration_code(self):
        async def fail_after_newer_request(*args):
            with self.sessions() as db:
                record = db.query(EmailVerificationCode).filter_by(email=BODY['email']).one()
                record.code = '999999'
                record.expires_at += timedelta(seconds=1)
                db.commit()
            raise TimeoutError()
        self.send.side_effect = fail_after_newer_request
        with patch('api.auth.secrets.randbelow', return_value=1):
            self.assertEqual(self.register().status_code, 503)
        self.assertEqual(self.code(), '999999')

    def test_existing_account_does_not_trigger_email(self):
        with self.sessions() as db:
            db.get(User, 1).email = 'existing@example.com'
            db.commit()
        response = self.client.post('/auth/request-register', json={'email':'existing@example.com', 'password':'unused'})
        self.assertEqual(response.status_code, 400)
        self.send.assert_not_awaited()

    def test_custom_domain_preflight_and_foreign_origin(self):
        for origin, allowed in ((ORIGIN, True), ('https://untrusted.example', False)):
            response = self.client.options('/auth/request-register', headers={
                'Origin':origin, 'Access-Control-Request-Method':'POST',
                'Access-Control-Request-Headers':'content-type'})
            self.assertEqual(response.status_code, 200 if allowed else 400)
