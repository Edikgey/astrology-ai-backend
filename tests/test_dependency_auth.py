"""JWT upgrade compatibility/security tests using synthetic local tokens only."""
import base64
import hashlib
import hmac
import json
import time
import unittest
from unittest.mock import patch
import test_my_charts as fixtures
from api import auth
from database.queries import User

SECRET = 'dependency-regression-private-test-secret-only'

def token(algorithm='HS256', secret=SECRET):
    def part(data):
        return base64.urlsafe_b64encode(json.dumps(data, separators=(',', ':')).encode()).rstrip(b'=')
    body = part({'alg': algorithm, 'typ': 'JWT'}) + b'.' + part({'sub': '1', 'ver': 0, 'exp': int(time.time()) + 300})
    digest = hashlib.sha384 if algorithm == 'HS384' else hashlib.sha256
    signature = base64.urlsafe_b64encode(hmac.new(secret.encode(), body, digest).digest()).rstrip(b'=')
    return (body+b'.'+signature).decode()

class DependencyAuthTests(unittest.TestCase):
    def setUp(self):
        fixtures.MyChartsTests.setUp(self)
        self.secret = patch.object(auth, 'SECRET_KEY', SECRET)
        self.secret.start()
    def tearDown(self):
        self.secret.stop()
        fixtures.MyChartsTests.tearDown(self)
    def test_existing_standard_hs256_token_remains_valid_after_upgrade(self):
        response = self.client.get('/auth/me', headers={'Authorization': 'Bearer '+token()})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['email'], 'a@example.test')
    def test_algorithm_allowlist_rejects_other_algorithms_and_unsigned_tokens(self):
        for algorithm in ('HS384', 'none', 'RS256', 'ES256'):
            with self.subTest(algorithm=algorithm):
                response = self.client.get('/auth/me', headers={'Authorization': 'Bearer '+token(algorithm)})
                self.assertEqual(response.status_code, 401)
    def test_public_key_like_secret_cannot_forge_private_hmac_token(self):
        response = self.client.get('/auth/me', headers={'Authorization': 'Bearer '+token(secret='public-key-is-not-the-server-secret')})
        self.assertEqual(response.status_code, 401)
    def test_auth_version_invalidation_is_preserved(self):
        with self.sessions() as db:
            db.get(User, 1).auth_version = 1
            db.commit()
        self.assertEqual(self.client.get('/auth/me', headers={'Authorization': 'Bearer '+token()}).status_code, 401)
