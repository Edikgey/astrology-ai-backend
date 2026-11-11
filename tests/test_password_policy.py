import string
import unittest
from unittest.mock import AsyncMock, patch
from pydantic import ValidationError
import test_my_charts as fixtures
from models.user import UserCreate, UserVerify, UserLogin
from database.queries import User, EmailVerificationCode
from api.auth import get_password_hash

VALID = ['Abcdef1!', 'Ab1!' + 'x' * 60, 'Correct-Horse9!']
INVALID = ['Abcde1!', 'Ab1!'+'x'*61, 'abcdef1!', 'ABCDEF1!', 'Abcdefg!',
           'Abcdef12', 'Abcdef1!я', 'Abcdef1!é', 'Abcdef1!中', 'Abcdef1!🙂',
           'Abcd 12!', 'Abcdef1!\n', 'Abcdef1!\t', 'Abcdef1!\u00a0', 'Abcdef1!\x00']

class PasswordPolicyTests(unittest.TestCase):
    def test_boundaries_and_all_ascii_specials(self):
        for password in VALID + ['Abcdef1'+c for c in string.punctuation]:
            for model in (UserCreate, UserVerify):
                with self.subTest(length=len(password), model=model.__name__):
                    self.assertEqual(model(email='a@example.com',password=password).password,password)

    def test_invalid_new_passwords_but_legacy_login_accepted_by_schema(self):
        for password in INVALID:
            for model in (UserCreate, UserVerify):
                with self.assertRaises(ValidationError):model(email='a@example.com',password=password)
            self.assertEqual(UserLogin(email='a@example.com',password=password).password,password)

class PasswordAPITests(unittest.TestCase):
    setUp = fixtures.MyChartsTests.setUp
    tearDown = fixtures.MyChartsTests.tearDown

    def test_both_registration_endpoints_reject_without_exposing_input_or_side_effects(self):
        with patch('api.auth.send_confirmation_email',new_callable=AsyncMock) as mail:
            for password in INVALID + [None, 123, '']:
                for endpoint in ('request-register','verify-code?code=123456'):
                    response=self.client.post('/auth/'+endpoint,json={'email':'new@example.com','password':password},headers={'Origin':'https://mylunariaai.com'})
                    self.assertEqual(response.status_code,422)
                    self.assertEqual(response.headers['access-control-allow-origin'],'https://mylunariaai.com')
                    self.assertTrue(all('input' not in e and 'ctx' not in e for e in response.json()['detail']))
                    if isinstance(password,str) and password:self.assertNotIn(password,response.text)
                    self.assertTrue(any(any('\u0400'<=c<='\u04ff' for c in e['msg']) for e in response.json()['detail']))
            mail.assert_not_awaited()
        with self.sessions() as db:
            self.assertEqual(db.query(User).count(),2)
            self.assertEqual(db.query(EmailVerificationCode).count(),0)

    def test_confirmation_mismatch_rejected_on_both_stages(self):
        for endpoint in ('request-register','verify-code?code=123456'):
            response=self.client.post('/auth/'+endpoint,json={'email':'new@example.com','password':VALID[0],'password_confirmation':'different'})
            self.assertEqual(response.status_code,422)
            self.assertEqual(response.json()['detail'][0]['msg'],'Пароли не совпадают.')
            self.assertNotIn(VALID[0],response.text)

    def test_eight_and_sixty_four_character_registration_hash_and_login(self):
        with patch('api.auth.send_confirmation_email',new_callable=AsyncMock):
            for i,password in enumerate(VALID[:2]):
                body={'email':f'new{i}@example.com','password':password,'password_confirmation':password}
                self.assertEqual(self.client.post('/auth/request-register',json=body).status_code,200)
                with self.sessions() as db:code=db.query(EmailVerificationCode).filter_by(email=body['email']).one().code
                response=self.client.post('/auth/verify-code',params={'code':code},json=body)
                self.assertEqual(response.status_code,200,response.text)
                with self.sessions() as db:
                    hashed=db.query(User).filter_by(email=body['email']).one().password_hash
                    self.assertNotEqual(hashed,password);self.assertTrue(hashed.startswith('$2'))
                self.assertEqual(self.client.post('/auth/login',json=body).status_code,200)

    def test_existing_weak_and_non_latin_passwords_still_log_in(self):
        for password in ('old', 'старый пароль', 'existing-password'):
            with self.sessions() as db:
                user=db.get(User,1);user.email='old@example.com';user.password_hash=get_password_hash(password);db.commit()
            response=self.client.post('/auth/login',json={'email':'old@example.com','password':password})
            self.assertEqual(response.status_code,200,response.text)

    def test_missing_password_error_is_russian_and_redacted(self):
        response=self.client.post('/auth/request-register',json={'email':'new@example.com'})
        self.assertEqual(response.json()['detail'][0]['msg'],'Введите пароль.')
