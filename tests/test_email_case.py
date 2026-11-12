"""BE-EMAIL-CASE regression: email sign-up and sign-in must not depend on letter case.

Written before the fix (tasks/BE-EMAIL-CASE/DESIGN.md, section 5). Isolated SQLite, no
.env, no real mail (send_confirmation_email is mocked), deterministic code via randbelow.

Traps this file avoids on purpose (each would make a test lie):
- legacy accounts are inserted WITHOUT an explicit id, so a primary-key collision can never
  stand in for the duplicate refusal (on PostgreSQL it would also end in 400);
- a second request-register of the same address in any case runs under a server clock
  shifted by +61 s, otherwise the 'register' email limit (1 per 60 s, casefold key) answers
  429 both before and after the fix;
- at most 10 sign-ins per address per test ('login' pair limit);
- Latin addresses only: SQLite lower() and the stand's PostgreSQL (initdb --locale=C) change
  only Latin letters.
"""
import logging
import unittest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

from sqlalchemy import func

import test_my_charts as fixtures
from api.auth import get_password_hash, verify_password
from database.queries import User

PASSWORD = 'Test-password1!'
OTHER_PASSWORD = 'Other-password2!'
CODE = '123456'  # 100000 + randbelow() -> randbelow patched to 23456
DUPLICATE = 'Этот email уже зарегистрирован. Войдите или восстановите пароль.'
LOGIN_REFUSED = 'Неверный email или пароль. Проверьте данные или восстановите пароль.'
LOG_MARKER = 'be-email-case-log-capture-marker'


class EmailCaseTests(unittest.TestCase):
    def setUp(self):
        fixtures.MyChartsTests.setUp(self)
        self.mail = patch('api.auth.send_confirmation_email', new_callable=AsyncMock)
        self.send = self.mail.start()
        self.code_source = patch('api.auth.secrets.randbelow', return_value=23456)
        self.code_source.start()

    def tearDown(self):
        self.code_source.stop()
        self.mail.stop()
        fixtures.MyChartsTests.tearDown(self)

    # --- helpers -------------------------------------------------------------------------
    def request_register(self, email, later_seconds=0, password=PASSWORD):
        body = {'email': email, 'password': password}
        if not later_seconds:
            return self.client.post('/auth/request-register', json=body)
        shifted = datetime.utcnow() + timedelta(seconds=later_seconds)
        with patch('modules.security_limits.server_now', return_value=shifted):
            return self.client.post('/auth/request-register', json=body)

    def verify(self, email, code=CODE, password=PASSWORD):
        return self.client.post('/auth/verify-code', json={'email': email, 'password': password, 'code': code})

    def register(self, email, password=PASSWORD):
        requested = self.request_register(email, password=password)
        self.assertEqual(requested.status_code, 200, requested.text)
        verified = self.verify(email, password=password)
        self.assertEqual(verified.status_code, 200, verified.text)
        return verified.json()['access_token']

    def login(self, email, password=PASSWORD):
        return self.client.post('/auth/login', json={'email': email, 'password': password})

    def me(self, token):
        response = self.client.get('/auth/me', headers={'Authorization': 'Bearer ' + token})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def legacy(self, email, password=PASSWORD):
        """An account created before the fix, stored exactly as typed. No explicit id."""
        with self.sessions() as db:
            user = User(email=email, password_hash=get_password_hash(password))
            db.add(user)
            db.commit()
            return user.id

    def accounts(self, canonical):
        """All rows whose email equals `canonical` ignoring (Latin) case."""
        with self.sessions() as db:
            rows = db.query(User).filter(func.lower(User.email) == canonical).order_by(User.id).all()
            return [(u.id, u.email, u.password_hash, u.auth_version) for u in rows]

    def mailed_to(self):
        return [str(call.args[0]) for call in self.send.await_args_list]

    def assert_duplicate(self, response):
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(response.json()['detail'], DUPLICATE)

    def assert_refused_login(self, response):
        self.assertEqual(response.status_code, 401, response.text)
        self.assertEqual(response.json()['detail'], LOGIN_REFUSED)
        self.assertNotIn('access_token', response.json())

    # --- T1-T8, T10 ----------------------------------------------------------------------
    def test_t1_login_in_other_case_reaches_new_account(self):
        """T1: registered as Case.User@Example.com, signs in as lower and upper case."""
        token = self.register('Case.User@Example.com')
        account_id = self.me(token)['id']
        for typed in ('case.user@example.com', 'CASE.USER@EXAMPLE.COM'):
            with self.subTest(typed=typed):
                response = self.login(typed)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(self.me(response.json()['access_token'])['id'], account_id)

    def test_t2_new_account_and_code_mail_use_lower_case(self):
        """T2: one account stored as case.user@example.com; the code went to that address."""
        self.register('Case.User@Example.com')
        stored = self.accounts('case.user@example.com')
        self.assertEqual([row[1] for row in stored], ['case.user@example.com'])
        self.assertEqual(self.mailed_to(), ['case.user@example.com'])

    def test_t3_request_register_in_other_case_is_refused_before_mail(self):
        """T3: Dup.User@ registered; request-register dup.user@ -> 400 with the fixed text, no mail."""
        self.register('Dup.User@example.com')
        self.assertEqual(self.send.await_count, 1)
        response = self.request_register('dup.user@example.com', later_seconds=61)
        self.assert_duplicate(response)
        self.assertEqual(self.send.await_count, 1)
        self.assertEqual(len(self.accounts('dup.user@example.com')), 1)

    def test_t4_legacy_mixed_case_account_signs_in_exactly_and_in_lower_case(self):
        """T4: legacy Legacy.User@ - exact spelling still works (guard), lower case reaches same id."""
        legacy_id = self.legacy('Legacy.User@example.com')
        exact = self.login('Legacy.User@example.com')
        self.assertEqual(exact.status_code, 200, exact.text)
        self.assertEqual(self.me(exact.json()['access_token'])['id'], legacy_id)
        lower = self.login('legacy.user@example.com')
        self.assertEqual(lower.status_code, 200, lower.text)
        self.assertEqual(self.me(lower.json()['access_token'])['id'], legacy_id)

    def test_t5a_existing_case_pair_never_crosses_accounts(self):
        """T5a (guard, green on base): a pre-existing pair keeps exact sign-in; nothing is guessed."""
        first = self.legacy('Pair.User@example.com', PASSWORD)
        second = self.legacy('pair.user@example.com', OTHER_PASSWORD)
        before = self.accounts('pair.user@example.com')
        self.assertEqual(len(before), 2)
        for typed, password, owner in (('Pair.User@example.com', PASSWORD, first),
                                       ('pair.user@example.com', OTHER_PASSWORD, second)):
            with self.subTest(typed=typed):
                response = self.login(typed, password)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(self.me(response.json()['access_token'])['id'], owner)
        # The exact spelling picked the first account; its neighbour's password is not tried.
        self.assert_refused_login(self.login('Pair.User@example.com', OTHER_PASSWORD))
        # A third spelling is ambiguous: no account is guessed, with either password.
        for password in (PASSWORD, OTHER_PASSWORD):
            with self.subTest(third_spelling_password=password):
                self.assert_refused_login(self.login('PAIR.USER@example.com', password))
        self.assertEqual(self.accounts('pair.user@example.com'), before)

    def test_t5b_existing_case_pair_blocks_new_registration(self):
        """T5b: with the pair present, request-register PAIR.user@ -> 400, no mail."""
        self.legacy('Pair.User@example.com', PASSWORD)
        self.legacy('pair.user@example.com', OTHER_PASSWORD)
        response = self.request_register('PAIR.user@example.com')
        self.assert_duplicate(response)
        self.send.assert_not_awaited()
        self.assertEqual(len(self.accounts('pair.user@example.com')), 2)

    def test_t6_account_appearing_between_code_and_verify_blocks_creation(self):
        """T6: code requested for late.user@, then Late.User@ appears, then verify -> 400, old row intact."""
        self.assertEqual(self.request_register('late.user@example.com').status_code, 200)
        legacy_id = self.legacy('Late.User@example.com', OTHER_PASSWORD)
        before = self.accounts('late.user@example.com')
        response = self.verify('late.user@example.com')
        self.assert_duplicate(response)
        self.assertEqual(self.accounts('late.user@example.com'), before)
        self.assertEqual(before[0][0], legacy_id)
        self.assertTrue(verify_password(OTHER_PASSWORD, before[0][2]))

    def test_t7_underscore_and_percent_are_not_patterns(self):
        """T7 (guard, green on base): '_' and '%' in an address are letters, never LIKE wildcards.

        Both directions: DESIGN's (stored '_', typed letter) and the one an ilike(typed) would
        actually break (typed '_' or '%' matching a stored letter).
        """
        self.legacy('under_score@example.com', PASSWORD)
        self.legacy('wildXcard@example.com', PASSWORD)
        self.legacy('percentXuser@example.com', PASSWORD)
        for typed in ('underXscore@example.com', 'wild_card@example.com', 'percent%user@example.com'):
            with self.subTest(typed=typed):
                self.assert_refused_login(self.login(typed, PASSWORD))
                response = self.request_register(typed)
                self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.send.await_count, 3)
        for stored in ('under_score@example.com', 'wildxcard@example.com', 'percentxuser@example.com'):
            self.assertEqual(len(self.accounts(stored)), 1)

    def test_t8_email_never_reaches_logs_or_refusal_bodies(self):
        """T8: a canary address is absent from the whole root log at DEBUG and from response bodies."""
        bodies = []
        with self.assertLogs(level='DEBUG') as logs:
            logging.getLogger('be_email_case').debug(LOG_MARKER)
            requested = self.request_register('Canary.Private@example.com')
            verified = self.verify('Canary.Private@example.com')
            bodies += [requested.text, verified.text]
            self.assertEqual((requested.status_code, verified.status_code), (200, 200), verified.text)
            repeat = self.request_register('canary.private@EXAMPLE.com', later_seconds=61)
            bodies.append(repeat.text)
            self.assert_duplicate(repeat)
            wrong = self.login('CANARY.private@example.com', OTHER_PASSWORD)
            bodies.append(wrong.text)
            self.assert_refused_login(wrong)
            right = self.login('CANARY.private@example.com', PASSWORD)
            bodies.append(right.text)
            self.assertEqual(right.status_code, 200, right.text)
        captured = '\n'.join(logs.output)
        self.assertIn(LOG_MARKER, captured)  # proves the capture saw DEBUG records
        self.assertNotIn('canary.private', captured.casefold())
        self.assertNotIn('canary.private', '\n'.join(bodies).casefold())

    def test_t10_newer_code_in_other_case_replaces_older_one(self):
        """T10: code 111111 for Code.User@, then 222222 for code.user@ -> old code refused, new accepted."""
        with patch('api.auth.secrets.randbelow', side_effect=[11111, 122222]):
            self.assertEqual(self.request_register('Code.User@example.com').status_code, 200)
            self.assertEqual(self.request_register('code.user@example.com', later_seconds=61).status_code, 200)
        old = self.verify('Code.User@example.com', code='111111')
        self.assertEqual(old.status_code, 400, old.text)
        new = self.verify('Code.User@example.com', code='222222')
        self.assertEqual(new.status_code, 200, new.text)
        self.assertEqual(len(self.accounts('code.user@example.com')), 1)


if __name__ == '__main__':
    unittest.main()
