"""Google verification uses real signed fixtures, offline Google certificate transport."""
import os
import time
import unittest
from unittest.mock import patch
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization
from google.auth import jwt, crypt
import test_my_charts as existing
from test_my_charts import User, get_password_hash

CLIENT = 'local-test.apps.googleusercontent.com'
NONCE = 'a' * 64
ORIGIN = 'https://astrology-ai-frontend-production.up.railway.app'
key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
private = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
public = key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
signer = crypt.RSASigner.from_string(private, key_id='test-key')


def credential(**changes):
    claims = dict(iss='https://accounts.google.com', aud=CLIENT, sub='google-123',
                  email='new@gmail.com', email_verified=True, nonce=NONCE,
                  iat=int(time.time()) - 10, exp=int(time.time()) + 600)
    claims.update(changes)
    return jwt.encode(signer, claims).decode()


class GoogleAuthTests(unittest.TestCase):
    chart = existing.MyChartsTests.chart

    def setUp(self):
        existing.MyChartsTests.setUp(self)
        with self.sessions() as db:
            db.get(User, 1).email = 'a@example.com'; db.commit()
        self.env = patch.dict(os.environ, {'GOOGLE_CLIENT_ID': CLIENT})
        self.certs = patch('google.oauth2.id_token._fetch_certs', return_value={'test-key': public})
        self.env.start(); self.certs.start()

    def tearDown(self):
        self.certs.stop(); self.env.stop()
        existing.MyChartsTests.tearDown(self)

    def login(self, token=None, **body):
        return self.client.post('/auth/google', json={'credential': token or credential(), 'nonce': NONCE, **body},
                                headers={'Origin': ORIGIN, **self.guest})

    def test_new_user_and_repeat_issue_working_application_jwt(self):
        first = self.login()
        self.assertEqual(first.status_code, 200, first.text)
        me = self.client.get('/auth/me', headers={'Authorization': 'Bearer ' + first.json()['access_token']})
        self.assertEqual(me.status_code, 200)
        user_id = me.json()['id']
        self.assertEqual(self.login().status_code, 200)
        with self.sessions() as db:
            self.assertEqual(db.query(User).count(), 3)
            user = db.get(User, user_id)
            self.assertEqual((user.google_sub, user.plan), ('google-123', 'free'))

    def test_verified_workspace_links_same_premium_user_preserves_password_and_data(self):
        saved = self.chart()
        hashed = get_password_hash('existing-password')
        with self.sessions() as db:
            user = db.get(User, 1); user.password_hash = hashed
            user.plan = 'premium'; user.provider_customer_id = 'customer'; user.subscription_status = 'active'
            db.commit()
        response = self.login(credential(email='a@example.com', hd='example.com'))
        self.assertEqual(response.status_code, 200, response.text)
        with self.sessions() as db:
            user = db.get(User, 1)
            self.assertEqual((user.password_hash, user.plan, user.provider_customer_id), (hashed, 'premium', 'customer'))
            self.assertEqual(user.google_sub, 'google-123')
            self.assertEqual(db.query(User).count(), 2)
        old_login = self.client.post('/auth/login', json={'email': 'a@example.com', 'password': 'existing-password'})
        self.assertEqual(old_login.status_code, 200)
        headers = {'Authorization': 'Bearer ' + response.json()['access_token']}
        self.assertEqual(self.client.get(f'/natal-chart/{saved}', headers=headers).status_code, 200)
        self.assertEqual(self.client.get(f'/gpt-messages?chart_id={saved}', headers=headers).status_code, 200)

    def test_third_party_email_requires_correct_existing_password(self):
        with self.sessions() as db:
            db.get(User, 1).password_hash = get_password_hash('existing-password'); db.commit()
        token = credential(email='a@example.com')
        self.assertEqual(self.login(token).json()['detail']['code'], 'google_link_confirmation_required')
        self.assertEqual(self.login(token, password='wrong').status_code, 401)
        with self.sessions() as db: self.assertIsNone(db.get(User, 1).google_sub)
        self.assertEqual(self.login(token, password='existing-password').status_code, 200)
        self.assertEqual(self.login(token).status_code, 200)
        with self.sessions() as db: self.assertEqual(db.query(User).count(), 2)

    def test_invalid_claims_and_signature_denied_without_user_creation(self):
        for changes in ({'aud': 'other-client'}, {'iss': 'https://evil.test'}, {'exp': 1},
                        {'email_verified': False}, {'sub': ''}, {'nonce': 'wrong'}, {'email': 'invalid'}, {'azp': 'other'}):
            with self.subTest(changes=changes): self.assertEqual(self.login(credential(**changes)).status_code, 401)
        self.assertEqual(self.login('invalid-token').status_code, 401)
        token = credential(); parts = token.split('.'); parts[-1] = 'a' * len(parts[-1])
        self.assertEqual(self.login('.'.join(parts)).status_code, 401)
        with self.sessions() as db: self.assertEqual(db.query(User).count(), 2)

    def test_subject_wins_when_google_email_changes(self):
        self.assertEqual(self.login().status_code, 200)
        self.assertEqual(self.login(credential(email='changed@gmail.com')).status_code, 200)
        with self.sessions() as db:
            self.assertEqual(db.query(User).count(), 3)
            self.assertEqual(db.query(User).filter(User.google_sub == 'google-123').one().email, 'new@gmail.com')

    def test_conflicting_provider_is_not_overwritten(self):
        with self.sessions() as db:
            db.get(User, 1).google_sub = 'different'; db.commit()
        self.assertEqual(self.login(credential(email='a@example.com', hd='example.com')).status_code, 409)

    def test_guest_only_selected_chart_and_retry_no_duplicate(self):
        selected = self.chart(user_id=None, session_token=self.guest_token)
        other = self.chart(user_id=None, session_token=self.guest_token)
        response = self.login(guest_chart_id=selected)
        self.assertEqual(response.json()['guest_chart_migration']['status'], 'migrated')
        again = self.login(guest_chart_id=selected)
        self.assertEqual(again.json()['guest_chart_migration']['status'], 'not_found')
        from database.queries import NatalChart
        with self.sessions() as db:
            self.assertIsNotNone(db.get(NatalChart, selected).user_id)
            self.assertIsNone(db.get(NatalChart, other).user_id)
            self.assertEqual(db.query(User).count(), 3)

    def test_google_guest_migration_respects_free_and_premium_limits(self):
        for plan, limit in [('free', 3), ('premium', 10)]:
            with self.subTest(plan=plan):
                with self.sessions() as db:
                    db.get(User, 1).plan = plan; db.commit()
                from database.queries import NatalChart
                with self.sessions() as db: count = db.query(NatalChart).filter(NatalChart.user_id == 1).count()
                for _ in range(limit - count): self.chart()
                selected = self.chart(user_id=None, session_token=self.guest_token)
                result = self.login(credential(email='a@example.com', hd='example.com'), guest_chart_id=selected)
                self.assertEqual(result.status_code, 200, result.text)
                self.assertEqual(result.json()['guest_chart_migration']['status'], 'limit_reached')
                with self.sessions() as db: self.assertIsNone(db.get(NatalChart, selected).user_id)

    def test_untrusted_origin_and_missing_configuration_fail_closed(self):
        response = self.client.post('/auth/google', json={'credential': credential(), 'nonce': NONCE}, headers={'Origin': 'https://evil.test'})
        self.assertEqual(response.status_code, 403)
        with patch.dict(os.environ, {'GOOGLE_CLIENT_ID': ''}): self.assertEqual(self.login().status_code, 503)

    def test_token_failure_rolls_back_link_and_guest_transfer(self):
        selected = self.chart(user_id=None, session_token=self.guest_token)
        with patch('api.auth.create_access_token', side_effect=RuntimeError('failure')):
            self.assertEqual(self.login(guest_chart_id=selected).status_code, 503)
        from database.queries import NatalChart
        with self.sessions() as db:
            self.assertEqual(db.query(User).count(), 2)
            self.assertIsNone(db.get(NatalChart, selected).user_id)
