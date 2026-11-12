"""BE-TEXTS regression: what the server says when it refuses (tasks/BE-TEXTS/DESIGN.md, sections 4 and 8).

Written before the fix. Every check first pins the status code that must NOT change (green on the
base commit), then the exact Russian text (red on the base commit: the server still sends the
"was" text - English, with emoji, with "Value error, ..." or without a next step).

Rules the texts must keep (contours 2 and 5):
- a payment whose outcome is unknown (P1, P2, P3 and the "already subscribed" refusal) says
  "Не оплачивайте повторно" and never "попробуйте"; "Попробуйте позже" is allowed only where nothing
  was created (P4, the billing portal);
- no emoji, no internal words, Cyrillic everywhere;
- a 422 never repeats what the user typed: every `msg` is a fixed string, and the answer keeps
  exactly the keys loc/type/msg.

Expected texts are literals (not imported from product code), like tests/test_email_case.py.
Synthetic data only: SQLite in memory, Stripe/Lava replaced by fakes, no network, no real keys.
"""
import json
import os
import unicodedata
import unittest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import httpx
import stripe
from fastapi import HTTPException

import test_lava as lava_fixtures
import test_my_charts as fixtures
import test_stripe as stripe_fixtures
from api.auth import get_password_hash, create_access_token
from database.queries import (BillingCustomer, GPTUsage, LavaCheckout, NatalChart, StripeCheckout, User)
from modules import lava_client, usage
from modules import stripe_billing as billing

# The real functions, captured before any test patches them.
REAL_STRIPE_API = billing.api
REAL_LAVA_REQUEST = lava_client.request

PASSWORD = 'Test-password1!'

# --- payment texts, DESIGN section 4 -----------------------------------------------------------
P1 = ('Проверяем оплату. Не оплачивайте повторно — обновите страницу через минуту. '
      'Если сообщение повторится, напишите в поддержку: lunariaai.support@gmail.com.')
P2 = ('Не удалось открыть или проверить оплату. Не оплачивайте повторно — обновите страницу через минуту. '
      'Если ошибка повторится, напишите в поддержку: lunariaai.support@gmail.com.')
P3 = ('Прошлую оплату нужно проверить вручную. Не оплачивайте повторно — напишите в поддержку: '
      'lunariaai.support@gmail.com.')
P4 = 'Stripe сейчас недоступен. Попробуйте позже.'
STRIPE_HAS_SUBSCRIPTION = ('У аккаунта уже есть подписка или незавершённая оплата. Не оплачивайте повторно — '
                           'обновите страницу через минуту. Если Premium не появится, напишите в поддержку: '
                           'lunariaai.support@gmail.com.')
STRIPE_BELONGS_TO_LAVA = ('Подписка этого аккаунта оформлена через Lava.top, поэтому оплата через Stripe недоступна. '
                          'Чтобы сменить способ оплаты, напишите в поддержку: lunariaai.support@gmail.com.')
STRIPE_STATUS_NOT_FOUND = 'Оплата не найдена. Обновите страницу.'
PORTAL_NO_SUBSCRIPTION = ('У аккаунта нет действующей подписки Stripe. Обновите страницу; если подписка оплачена, '
                          'напишите в поддержку: lunariaai.support@gmail.com.')
PORTAL_NOT_CONFIGURED = 'Управление подпиской временно недоступно. Попробуйте позже.'
# Unchanged by this task (already Russian, with a next step); pinned so they stay that way.
PORTAL_UNAVAILABLE_KEPT = 'Подписка Stripe недоступна для управления. Обновите данные аккаунта.'
PORTAL_OPEN_FAILED_KEPT = 'Не удалось открыть управление подпиской. Попробуйте позже.'

LAVA_ALREADY_PAID = 'Подписка уже оплачена — повторная оплата не нужна. Обновите страницу через минуту.'
LAVA_BELONGS_TO_STRIPE = ('Оплата в рублях для этого аккаунта недоступна: у него уже есть подписка или оплата через '
                          'Stripe. Чтобы сменить способ оплаты, напишите в поддержку: lunariaai.support@gmail.com.')
LAVA_NO_SUBSCRIPTION = 'У аккаунта нет подписки через Lava.top — отключать нечего. Обновите страницу.'
LAVA_CREATION_PENDING_KEPT = 'Создание оплаты ожидает проверки. Обратитесь в поддержку перед повторной оплатой.'

# --- modules/lava_client.py, DESIGN section 12.1 ----------------------------------------------
PROVIDER_SILENT = ('Платёжный сервис не ответил. Обновите страницу через минуту и проверьте статус, прежде чем '
                   'снова оплачивать или отменять подписку. Если ошибка повторится, напишите в поддержку: '
                   'lunariaai.support@gmail.com.')
RUB_NOT_AVAILABLE = 'Оплата в рублях пока недоступна. Попробуйте позже.'
RUB_TEMPORARILY_UNAVAILABLE = 'Оплата в рублях временно недоступна. Попробуйте позже.'
LAVA_EMAIL_REJECTED_KEPT = ('Lava отклонила email этого аккаунта для покупки. Обратитесь в поддержку Lunaria: '
                            'lunariaai.support@gmail.com. Оплата не создана.')

# --- cards, sign-in, relationships, DESIGN section 4.3 ----------------------------------------
CHART_NOT_CALCULATED = 'Не удалось рассчитать карту. Попробуйте ещё раз через минуту.'
CHART_NOT_FOUND = 'Карта не найдена. Откройте её из раздела «Мои карты» или постройте новую.'
CHART_DATA_MISSING = 'Расчётные данные карты отсутствуют. Обратитесь в поддержку для восстановления карты.'
CHART_DELETE_NOT_FOUND = 'Карта не найдена — возможно, она уже удалена. Обновите страницу.'
CHART_FORBIDDEN = 'Нет доступа к этой карте. Откройте свою карту из раздела «Мои карты».'
AI_EMPTY_ANSWER = 'AI не вернул ответ. Квота не списана — задайте вопрос ещё раз.'
CODE_REFUSED = 'Неверный или просроченный код. Проверьте код из письма или запросите новый.'
LOGIN_REFUSED = 'Неверный email или пароль. Проверьте данные или восстановите пароль.'
GUEST_SESSION = 'Не удалось распознать гостевую сессию. Обновите страницу и попробуйте снова.'
NOT_SIGNED_IN = 'Войдите в аккаунт или обновите страницу, чтобы продолжить.'
GOOGLE_ORIGIN = 'Вход через Google работает только на сайте mylunariaai.com. Откройте сайт и войдите снова.'
GOOGLE_BAD_REQUEST = 'Не удалось обработать вход через Google. Обновите страницу и попробуйте снова.'
GOOGLE_WRONG_PASSWORD = 'Неверный пароль. Введите пароль от вашего аккаунта Lunaria или восстановите его.'
SESSION_EXPIRED = 'Сессия истекла. Войдите в аккаунт снова.'
ACCOUNT_NOT_FOUND = 'Аккаунт не найден. Войдите снова или зарегистрируйтесь.'
RELATIONSHIP_NOT_FOUND = 'Анализ отношений не найден. Обновите страницу и выберите анализ из списка.'

# --- 422, DESIGN section 4.4 -------------------------------------------------------------------
EMAIL_FORMAT = 'Введите email в формате name@example.com.'
CODE_FORMAT = 'Введите 6 цифр кода из письма.'
RESET_LINK_INVALID = 'Ссылка недействительна или срок её действия истёк. Запросите новую ссылку.'
CHECK_INPUT = 'Проверьте введённые данные и попробуйте ещё раз.'
ENTER_PASSWORD = 'Введите пароль.'
CHARTS_DIFFER = 'Выберите две разные натальные карты.'
LABEL_RULE = 'Укажите имя от 1 до 100 символов без управляющих символов.'
LOCATION_CHANGED = 'Место рождения изменилось. Выберите его из подсказок заново.'
PASSWORDS_DIFFER = 'Пароли не совпадают.'

ENGLISH_VALIDATION = ('Value error', 'Input should', 'String should', 'Field required', 'value is not a valid',
                      'Extra inputs')


def has_emoji(text):
    return any(unicodedata.category(c) == 'So' or 0x1F000 <= ord(c) <= 0x1FAFF for c in text)


class Texts:
    """Shared assertions. Status first (must not change), then the exact text, then the rules."""

    def refused(self, response, status, expected, *, unknown_outcome=False):
        """HTTP answer: same status code as before, `detail` equal to the new text."""
        self.assertEqual(response.status_code, status, response.text)
        detail = response.json()['detail']
        self.assertIsInstance(detail, str, response.text)
        self.assertEqual(detail, expected)
        self.assertRegex(detail, '[А-Яа-яЁё]')
        self.assertFalse(has_emoji(detail), detail)
        if unknown_outcome:
            self.assertIn('Не оплачивайте повторно', detail)
            self.assertNotIn('попробуйте', detail.lower())

    def raised(self, error, status, expected, *, unknown_outcome=False):
        """HTTPException raised by a helper function."""
        self.assertEqual(error.status_code, status, error.detail)
        self.assertEqual(error.detail, expected)
        self.assertRegex(error.detail, '[А-Яа-яЁё]')
        self.assertFalse(has_emoji(error.detail), error.detail)
        if unknown_outcome:
            self.assertIn('Не оплачивайте повторно', error.detail)
            self.assertNotIn('попробуйте', error.detail.lower())



# =============================================================================================
# Stripe (T1-T7): modules/stripe_billing.py
# =============================================================================================
class StripeRefusalTexts(Texts, unittest.TestCase):
    tearDown = fixtures.MyChartsTests.tearDown
    send = stripe_fixtures.StripeTests.send

    def setUp(self):
        stripe_fixtures.StripeTests.setUp(self)

    def checkout(self, headers=None):
        return self.client.post('/payments/stripe/checkout', headers=headers or self.owner)

    def portal(self, headers=None):
        return self.client.post('/payments/stripe/portal', headers=headers or self.owner)

    def make_premium(self, user_id=1, provider=None):
        with self.sessions() as db:
            user = db.get(User, user_id)
            user.payment_provider = provider
            usage.apply_plan(db, user, 'premium', self.start, self.end)
            db.commit()

    # --- T1, T2: the paid session still waits for the provider's confirmation ----------------
    def test_t1_complete_session_waits_for_confirmation(self):
        self.session['status'] = 'complete'
        self.refused(self.checkout(), 409, P1, unknown_outcome=True)
        self.api.checkout.sessions.create.assert_not_called()

    def test_t2_expired_session_with_subscription_is_being_confirmed(self):
        self.session['status'] = 'expired'  # the intent 'bound' already holds a subscription id
        self.refused(self.checkout(), 409, P1, unknown_outcome=True)
        self.api.checkout.sessions.create.assert_not_called()

    # --- T3: provider failure, outcome unknown ------------------------------------------------
    def test_t3_provider_failure_says_do_not_pay_again(self):
        self.api.checkout.sessions.create.side_effect = RuntimeError('do not expose provider response')
        for attempt in range(2):
            with self.subTest(attempt=attempt):
                response = self.checkout(self.other)
                self.refused(response, 502, P2, unknown_outcome=True)
                self.assertNotIn('do not expose', response.text)
        keys = [c.kwargs['options']['idempotency_key'] for c in self.api.checkout.sessions.create.call_args_list]
        self.assertEqual(len(set(keys)), 1)
        with self.sessions() as db:
            self.assertEqual(db.query(StripeCheckout).filter_by(user_id=2).count(), 1)

    # --- T4: needs a manual check, never fixes itself -----------------------------------------
    def test_t4_stale_intent_needs_manual_check(self):
        with self.sessions() as db:
            row = db.get(StripeCheckout, 'bound')
            row.session_id = None
            row.created_at = datetime.utcnow() - timedelta(days=2)
            db.commit()
        self.refused(self.checkout(), 409, P3, unknown_outcome=True)
        self.api.checkout.sessions.create.assert_not_called()
        self.api.customers.create.assert_not_called()

    def test_t4_stale_customer_without_id_needs_manual_check(self):
        with self.sessions() as db:
            db.add(BillingCustomer(user_id=2, provider='stripe', account_id=stripe_fixtures.ACCOUNT, livemode=True,
                                   customer_id=None, created_at=datetime.utcnow() - timedelta(days=2)))
            db.commit()
        self.refused(self.checkout(self.other), 409, P3, unknown_outcome=True)
        self.api.checkout.sessions.create.assert_not_called()
        self.api.customers.create.assert_not_called()

    def test_t4_subscription_in_inconsistent_state_needs_manual_check(self):
        with self.sessions() as db:
            user = db.get(User, 1)
            user.payment_provider, user.provider_subscription_id = 'stripe', 'sub_unreconciled'  # status stays None
            db.commit()
        self.refused(self.checkout(), 409, P3, unknown_outcome=True)
        self.api.checkout.sessions.create.assert_not_called()
        self.api.customers.create.assert_not_called()

    # --- T5: already subscribed / belongs to the other provider -------------------------------
    def test_t5_premium_user_must_not_pay_again(self):
        self.make_premium()
        self.refused(self.checkout(), 409, STRIPE_HAS_SUBSCRIPTION, unknown_outcome=True)
        self.api.checkout.sessions.create.assert_not_called()

    def test_t5_past_due_subscription_must_not_pay_again(self):
        with self.sessions() as db:
            db.get(User, 2).subscription_status = 'past_due'
            db.commit()
        self.refused(self.checkout(self.other), 409, STRIPE_HAS_SUBSCRIPTION, unknown_outcome=True)

    def test_t5_account_billed_through_lava(self):
        self.make_premium(provider='lava')
        with self.sessions() as db:
            db.get(User, 1).provider_subscription_id = 'lava-primary'
            db.commit()
        self.refused(self.checkout(), 409, STRIPE_BELONGS_TO_LAVA)

    # --- T6: nothing was created yet - "try later" is allowed ---------------------------------
    def test_t6_checkout_switched_off(self):
        with patch.dict(os.environ, {'STRIPE_CHECKOUT_ENABLED': 'false'}):
            self.refused(self.checkout(), 503, P4)
        self.api.checkout.sessions.create.assert_not_called()

    def test_t6_not_live_configuration_blocks_the_portal(self):
        with patch.dict(os.environ, {'STRIPE_LIVEMODE': 'false'}):
            self.refused(self.portal(), 503, P4)

    def test_t6_checkout_return_url_missing(self):
        with patch.dict(os.environ, {'STRIPE_CHECKOUT_RETURN_URL': ''}):
            self.refused(self.checkout(), 503, P4)
        self.api.checkout.sessions.create.assert_not_called()

    def test_t6_return_url_helper_without_variable(self):
        with self.assertRaises(HTTPException) as caught:
            billing.return_url('NO_SUCH_VAR')
        self.raised(caught.exception, 503, P4)

    def test_t6_price_not_as_reviewed(self):
        self.api.prices.retrieve.side_effect = lambda _: {**stripe_fixtures.price(), 'unit_amount': 1000}
        self.refused(self.checkout(), 503, P4)
        self.api.checkout.sessions.create.assert_not_called()
        with self.assertRaises(HTTPException) as caught:
            billing.validate_price({**stripe_fixtures.price(), 'unit_amount': 1000})
        self.raised(caught.exception, 503, P4)

    def test_t6_other_stripe_account(self):
        with patch.object(billing, 'api', REAL_STRIPE_API), patch('stripe.StripeClient') as client:
            client.return_value.raw_request.return_value.data = {'id': 'acct_someone_else'}
            self.refused(self.checkout(), 503, P4)
            with self.assertRaises(HTTPException) as caught:
                billing.api()
        self.raised(caught.exception, 503, P4)
        self.api.checkout.sessions.create.assert_not_called()

    def test_t6_stripe_unreachable_before_anything_is_created(self):
        with patch.object(billing, 'api', REAL_STRIPE_API), patch('stripe.StripeClient') as client:
            client.return_value.raw_request.side_effect = stripe.APIConnectionError('unreachable')
            self.refused(self.checkout(), 503, P4)
            with self.assertRaises(HTTPException) as caught:
                billing.api()
        self.raised(caught.exception, 503, P4)
        self.api.checkout.sessions.create.assert_not_called()

    # --- T7: portal and status ----------------------------------------------------------------
    def test_t7_portal_without_stripe_subscription(self):
        self.refused(self.portal(self.other), 409, PORTAL_NO_SUBSCRIPTION)
        self.api.billing_portal.sessions.create.assert_not_called()

    def test_t7_portal_not_configured(self):
        self.send()  # activates Premium for user 1 through the signed webhook
        with patch.dict(os.environ, {'STRIPE_PORTAL_CONFIGURATION_ID': ''}):
            self.refused(self.portal(), 503, PORTAL_NOT_CONFIGURED)
        self.api.billing_portal.sessions.create.assert_not_called()

    def test_t7_status_of_missing_or_foreign_checkout(self):
        for session_id, headers in ((stripe_fixtures.SESSION, self.other), ('cs_live_no_such_session', self.owner)):
            with self.subTest(session_id=session_id):
                response = self.client.get('/payments/stripe/checkout/' + session_id, headers=headers)
                self.refused(response, 404, STRIPE_STATUS_NOT_FOUND)

    # --- unchanged texts, pinned so they stay Russian with a next step ------------------------
    def test_portal_texts_kept(self):
        self.send()
        self.sub['customer'] = 'cus_other'
        self.refused(self.portal(), 409, PORTAL_UNAVAILABLE_KEPT)
        self.sub['customer'] = stripe_fixtures.CUSTOMER
        self.api.billing_portal.sessions.create.side_effect = RuntimeError('do not expose')
        self.refused(self.portal(), 502, PORTAL_OPEN_FAILED_KEPT)


# =============================================================================================
# Lava (T8-T10): modules/lava_billing.py
# =============================================================================================
class LavaRefusalTexts(Texts, unittest.TestCase):
    tearDown = fixtures.MyChartsTests.tearDown
    provider = lava_fixtures.LavaTests.provider

    def setUp(self):
        lava_fixtures.LavaTests.setUp(self)

    def checkout(self, headers=None):
        return self.client.post('/payments/lava/checkout', headers=headers or self.owner)

    def posted(self):
        return [c for c in self.network.call_args_list if c.args[0] == 'POST']

    def invoice_status(self, status, buyer='a@example.test'):
        def answer(method, path, **kwargs):
            if method == 'GET' and path.startswith('/api/v2/invoices/'):
                return {'id': path.rsplit('/', 1)[1], 'buyer': {'email': buyer}, 'status': status}
            return self.provider(method, path, **kwargs)
        self.network.side_effect = answer

    # --- T8: the invoice call failed, outcome unknown -----------------------------------------
    def test_t8_second_payment_while_first_is_unconfirmed(self):
        def fail(method, path, **kwargs):
            if method == 'POST':
                raise HTTPException(502, 'provider unavailable')
            return self.provider(method, path, **kwargs)
        self.network.side_effect = fail
        self.assertEqual(self.checkout(self.other).status_code, 502)
        self.refused(self.checkout(self.other), 409, P1, unknown_outcome=True)
        self.assertEqual(len(self.posted()), 1)

    def test_t8_provider_silent_on_invoice_creation_then_second_attempt(self):
        """Through the real lava_client.request: the first answer already says "проверяем оплату"."""
        original = httpx.Client

        def handler(request):
            if request.method == 'POST':
                return httpx.Response(500, json={})
            if request.url.path == '/api/v2/products':
                return httpx.Response(200, json=self.catalog)
            return httpx.Response(404, json={})

        self.network.side_effect = lambda method, path, **kwargs: REAL_LAVA_REQUEST(method, path, **kwargs)
        with patch('modules.lava_client.httpx.Client',
                   side_effect=lambda **kwargs: original(transport=httpx.MockTransport(handler))):
            first = self.checkout(self.other)
            second = self.checkout(self.other)
        self.refused(first, 502, PROVIDER_SILENT)
        self.refused(second, 409, P1, unknown_outcome=True)
        with self.sessions() as db:
            self.assertEqual(db.query(LavaCheckout).filter_by(user_id=2).count(), 1)

    def test_t8_creation_pending_text_kept(self):
        self.refused(self.checkout(), 409, LAVA_CREATION_PENDING_KEPT)  # 'bound' has no payment url yet

    # --- T9: reconciliation before a new payment ----------------------------------------------
    def test_t9_reconciliation_reply_does_not_match(self):
        def foreign(method, path, **kwargs):
            if method == 'GET' and path.startswith('/api/v2/invoices/'):
                return {'id': str(uuid4()), 'buyer': {'email': 'a@example.test'}, 'status': 'NEW'}
            return self.provider(method, path, **kwargs)
        self.network.side_effect = foreign
        self.refused(self.checkout(), 502, P2, unknown_outcome=True)
        self.assertEqual(self.posted(), [])

    def test_t9_already_paid(self):
        with self.sessions() as db:
            db.get(LavaCheckout, 'bound').paid_at = datetime.utcnow()
            db.commit()
        self.refused(self.checkout(), 409, LAVA_ALREADY_PAID)
        self.assertEqual(self.posted(), [])

    def test_t9_provider_says_paid_but_we_have_not_seen_it(self):
        self.invoice_status('COMPLETED')
        self.refused(self.checkout(), 409, P1, unknown_outcome=True)
        self.assertEqual(self.posted(), [])

    def test_t9_failed_attempt_not_confirmed_by_provider(self):
        with self.sessions() as db:
            db.get(LavaCheckout, 'bound').state = 'failed'
            db.commit()
        self.invoice_status('NEW')
        self.refused(self.checkout(), 409, P1, unknown_outcome=True)
        self.assertEqual(self.posted(), [])

    # --- T10 ----------------------------------------------------------------------------------
    def test_t10_account_billed_through_stripe(self):
        with self.sessions() as db:
            db.get(User, 1).payment_provider = 'stripe'
            db.commit()
        self.refused(self.checkout(), 409, LAVA_BELONGS_TO_STRIPE)
        self.assertEqual(self.posted(), [])

    def test_t10_cancel_without_lava_subscription(self):
        response = self.client.post('/payments/lava/cancel', headers=self.other)
        self.refused(response, 409, LAVA_NO_SUBSCRIPTION)
        self.assertFalse(any(c.args[0] == 'DELETE' for c in self.network.call_args_list))


# =============================================================================================
# Lava provider client (DESIGN section 12.1): modules/lava_client.py
# =============================================================================================
class LavaClientRefusalTexts(Texts, unittest.TestCase):
    def call(self, response, method='POST', path='/api/v3/invoice'):
        original = httpx.Client
        handler = Mock(side_effect=response) if isinstance(response, Exception) else Mock(return_value=response)
        client = original(transport=httpx.MockTransport(handler))
        with patch.dict(os.environ, lava_fixtures.ENV), patch('modules.lava_client.httpx.Client', return_value=client):
            with self.assertRaises(HTTPException) as caught:
                lava_client.request(method, path, json={})
        self.assertEqual(handler.call_count, 1)  # still no automatic retry
        return caught.exception

    def test_provider_failures_say_check_before_paying_or_cancelling(self):
        cases = [('rate limit', httpx.Response(429, json={}), 503),
                 ('server error', httpx.Response(500, json={}), 502),
                 ('not json', httpx.Response(200, content=b'bad'), 502),
                 ('redirect', httpx.Response(302, content=b''), 502),
                 ('timeout', httpx.ReadTimeout('slow'), 502)]
        for method, path in (('POST', '/api/v3/invoice'), ('GET', '/api/v2/invoices/' + lava_fixtures.CONTRACT),
                             ('DELETE', '/api/v1/subscriptions')):
            for name, response, status in cases:
                with self.subTest(method=method, case=name):
                    error = self.call(response, method, path)
                    self.raised(error, status, PROVIDER_SILENT)
                    self.assertNotIn('попробуйте', error.detail.lower())

    def test_rate_limit_keeps_retry_after_header(self):
        error = self.call(httpx.Response(429, json={}))
        self.raised(error, 503, PROVIDER_SILENT)
        self.assertEqual(error.headers, {'Retry-After': '60'})

    def test_email_rejection_text_kept(self):
        error = self.call(httpx.Response(400, json={'error': 'Incorrect email to purchase'}))
        self.raised(error, 422, LAVA_EMAIL_REJECTED_KEPT)

    def test_not_configured(self):
        for name, env in (('no api key', {'LAVA_API_KEY': ''}), ('bad offer id', {'LAVA_OFFER_ID': 'not-a-uuid'}),
                          ('short secret', {'LAVA_WEBHOOK_SECRET': 'short'})):
            with self.subTest(case=name), patch.dict(os.environ, {**lava_fixtures.ENV, **env}):
                with self.assertRaises(HTTPException) as caught:
                    lava_client.request('GET', '/api/v2/products') if name == 'no api key' else lava_client.config()
                self.raised(caught.exception, 503, RUB_NOT_AVAILABLE)

    def test_catalog_problems_stop_before_any_invoice(self):
        offer, product = lava_fixtures.OFFER, lava_fixtures.PRODUCT

        def catalog(amount):
            return {'items': [{'id': product, 'type': 'SUBSCRIPTION', 'offers': [{'id': offer, 'prices': [
                {'currency': 'RUB', 'amount': amount, 'periodicity': 'MONTHLY'}]}]}]}

        cases = [('price changed', catalog(100), 503), ('offer not found', {'items': []}, 503),
                 ('foreign next page', {'items': [], 'nextPage': 'https://evil.example/api/v2/products?x=1'}, 502)]
        for name, body, status in cases:
            with self.subTest(case=name), patch('modules.lava_client.request', return_value=body) as request:
                with self.assertRaises(HTTPException) as caught:
                    lava_client.verified_offer(offer)
                self.raised(caught.exception, status, RUB_TEMPORARILY_UNAVAILABLE)
                self.assertFalse(any(c.args[0] == 'POST' for c in request.call_args_list))


# =============================================================================================
# Sign-in, cards, relationships (T11-T15)
# =============================================================================================
class AccountRefusalTexts(Texts, unittest.TestCase):
    chart = fixtures.MyChartsTests.chart

    def setUp(self):
        fixtures.MyChartsTests.setUp(self)

    tearDown = fixtures.MyChartsTests.tearDown

    def known_user(self, email='known@example.com'):
        with self.sessions() as db:
            db.add(User(email=email, password_hash=get_password_hash(PASSWORD)))
            db.commit()

    # --- T11 ----------------------------------------------------------------------------------
    def test_t11_wrong_code(self):
        response = self.client.post('/auth/verify-code', json={'email': 'fresh@example.com', 'password': PASSWORD,
                                                                'code': '123456'})
        self.refused(response, 400, CODE_REFUSED)

    def test_t11_sign_in_refusal_is_one_text_for_every_reason(self):
        self.known_user()
        wrong_password = self.client.post('/auth/login', json={'email': 'known@example.com', 'password': 'Other-password2!'})
        unknown_email = self.client.post('/auth/login', json={'email': 'nobody@example.com', 'password': PASSWORD})
        self.refused(wrong_password, 401, LOGIN_REFUSED)
        self.refused(unknown_email, 401, LOGIN_REFUSED)
        self.assertEqual(wrong_password.json(), unknown_email.json())

    def test_t11_unreadable_guest_session_on_sign_in(self):
        self.known_user()
        response = self.client.post('/auth/login', json={'email': 'known@example.com', 'password': PASSWORD},
                                    headers={'X-Session-Token': 'zz'})
        self.refused(response, 400, GUEST_SESSION)

    # --- T12 ----------------------------------------------------------------------------------
    GOOGLE_BODY = {'credential': 'not-a-real-credential', 'nonce': 'a' * 32}

    def test_t12_google_foreign_origin(self):
        response = self.client.post('/auth/google', json=self.GOOGLE_BODY, headers={'Origin': 'https://evil.example'})
        self.refused(response, 403, GOOGLE_ORIGIN)

    def test_t12_google_not_json(self):
        response = self.client.post('/auth/google', content=json.dumps(self.GOOGLE_BODY),
                                    headers={'Origin': 'https://mylunariaai.com',
                                             'Content-Type': 'application/merge-patch+json'})
        self.refused(response, 415, GOOGLE_BAD_REQUEST)

    def test_t12_google_link_with_wrong_password(self):
        self.known_user()
        identity = {'sub': 'google-sub-1', 'email': 'known@example.com', 'authoritative': False}
        with patch('api.auth.verify_google_credential', return_value=identity):
            response = self.client.post('/auth/google', json={**self.GOOGLE_BODY, 'password': 'Other-password2!'},
                                        headers={'Origin': 'https://mylunariaai.com'})
        self.refused(response, 401, GOOGLE_WRONG_PASSWORD)

    # --- T13 ----------------------------------------------------------------------------------
    def test_t13_unreadable_token(self):
        response = self.client.get('/auth/me', headers={'Authorization': 'Bearer garbage'})
        self.refused(response, 401, SESSION_EXPIRED)

    def test_t13_token_of_a_missing_account(self):
        token = create_access_token({'sub': '999'})
        response = self.client.get('/auth/me', headers={'Authorization': 'Bearer ' + token})
        self.refused(response, 404, ACCOUNT_NOT_FOUND)

    def test_t13_unreadable_guest_session_on_a_card(self):
        response = self.client.get('/natal-chart/1', headers={'X-Session-Token': 'zz'})
        self.refused(response, 400, GUEST_SESSION)

    def test_t13_no_credentials_on_a_card(self):
        self.refused(self.client.get('/natal-chart/1'), 401, NOT_SIGNED_IN)

    # --- T14 ----------------------------------------------------------------------------------
    def test_t14_card_missing_or_foreign(self):
        foreign = self.chart(user_id=2)
        for chart_id in (99999, foreign):
            with self.subTest(chart_id=chart_id):
                self.refused(self.client.get(f'/natal-chart/{chart_id}', headers=self.owner), 404, CHART_NOT_FOUND)

    def test_t14_card_without_calculation_data(self):
        chart_id = self.chart(related=False)
        self.refused(self.client.get(f'/natal-chart/{chart_id}', headers=self.owner), 404, CHART_DATA_MISSING)

    def test_t14_delete_missing_or_foreign_card(self):
        foreign = self.chart(user_id=2)
        for chart_id in (99999, foreign):
            with self.subTest(chart_id=chart_id):
                self.refused(self.client.delete(f'/natal-chart/{chart_id}', headers=self.owner), 404,
                             CHART_DELETE_NOT_FOUND)

    def test_t14_chat_and_history_for_missing_or_foreign_card(self):
        foreign = self.chart(user_id=2)
        ask = lambda chart_id: self.client.post('/ask-gpt', headers=self.owner,
                                                json={'chart_id': chart_id, 'question': 'Что со мной?'})
        history = lambda chart_id: self.client.get(f'/gpt-messages?chart_id={chart_id}', headers=self.owner)
        for name, call in (('ask', ask), ('history', history)):
            with self.subTest(call=name, case='missing'):
                self.refused(call(99999), 404, CHART_NOT_FOUND)
            with self.subTest(call=name, case='foreign'):
                self.refused(call(foreign), 403, CHART_FORBIDDEN)

    def test_t14_empty_ai_answer_releases_the_quota(self):
        chart_id = self.chart(related=False)
        with patch('api.endpoints.ChartInterpreter') as interpreter:
            interpreter.return_value.ask_gpt = AsyncMock(return_value='   ')
            response = self.client.post('/ask-gpt', headers=self.owner,
                                        json={'chart_id': chart_id, 'question': 'Что со мной?'})
        self.refused(response, 502, AI_EMPTY_ANSWER)
        with self.sessions() as db:
            statuses = {row.status for row in db.query(GPTUsage).filter_by(user_id=1)}
        self.assertEqual(statuses, {'released'})  # one reservation, given back: nothing counted

    def test_t14_no_bodies_from_the_calculation(self):
        with patch('api.endpoints.Ephemeris') as ephemeris:
            ephemeris.return_value.get_all_bodies_with_degrees.return_value = {}
            ephemeris.return_value.get_all_bodies_with_symbols.return_value = {}
            response = self.client.post('/natal-chart', json=fixtures.PAYLOAD, headers=self.owner)
        self.refused(response, 400, CHART_NOT_CALCULATED)
        with self.sessions() as db:
            self.assertEqual(db.query(NatalChart).count(), 0)  # nothing saved

    # --- T15 ----------------------------------------------------------------------------------
    def test_t15_relationship_missing(self):
        self.refused(self.client.get('/relationships/99999', headers=self.owner), 404, RELATIONSHIP_NOT_FOUND)
        self.refused(self.client.delete('/relationships/99999', headers=self.owner), 404, RELATIONSHIP_NOT_FOUND)


# =============================================================================================
# 422 (T16): main.py validation handler
# =============================================================================================
class ValidationTexts(unittest.TestCase):
    """Same shape as before - a list of {loc, type, msg}, status 422 - but msg is a fixed Russian text."""
    tearDown = fixtures.MyChartsTests.tearDown

    def setUp(self):
        fixtures.MyChartsTests.setUp(self)

    def assert_422(self, response, expected, typed=()):
        """expected: [(loc, type, msg), ...] in answer order. `typed`: user input that must not come back."""
        self.assertEqual(response.status_code, 422, response.text)
        detail = response.json()['detail']
        self.assertEqual([(item['loc'], item['type']) for item in detail],
                         [(loc, kind) for loc, kind, _ in expected], response.text)  # unchanged shape
        self.assertEqual([item['msg'] for item in detail], [msg for _, _, msg in expected])
        for item in detail:
            self.assertEqual(set(item), {'loc', 'type', 'msg'})  # no `input`, no `ctx`
            self.assertRegex(item['msg'], '[А-Яа-яЁё]')
            self.assertFalse(has_emoji(item['msg']), item['msg'])
            for english in ENGLISH_VALIDATION:
                self.assertNotIn(english, item['msg'])
        for value in typed:
            self.assertNotIn(value, response.text)

    def test_t16_email_on_register_login_and_reset_request(self):
        paths = (('/auth/request-register', {'password': PASSWORD}), ('/auth/login', {'password': PASSWORD}),
                 ('/auth/request-password-reset', {}))
        values = (('not-an-email', 'value_error'), ('a,b@example.com', 'value_error'), (5, 'string_type'),
                  (None, 'missing'))
        for path, extra in paths:
            for value, kind in values:
                body = dict(extra)
                if value is not None:
                    body['email'] = value
                with self.subTest(path=path, email=value):
                    response = self.client.post(path, json=body)
                    typed = [value] if isinstance(value, str) else []
                    self.assert_422(response, [(['body', 'email'], kind, EMAIL_FORMAT)], typed + ['a,b', "','"])

    def test_t16_code_on_verify(self):
        values = (('12345', 'string_too_short'), ('12a456', 'string_pattern_mismatch'),
                  ('1234567', 'string_too_long'), (123456, 'string_type'))
        for value, kind in values:
            with self.subTest(code=value):
                response = self.client.post('/auth/verify-code', json={
                    'email': 'fresh@example.com', 'password': PASSWORD, 'code': value})
                self.assert_422(response, [(['body', 'code'], kind, CODE_FORMAT)], ['12a456', '1234567', '^\\d'])

    def test_t16_unreadable_guest_session_header(self):
        response = self.client.post('/auth/verify-code', headers={'X-Session-Token': 'zz'}, json={
            'email': 'fresh@example.com', 'password': PASSWORD, 'code': '123456'})
        self.assert_422(response, [(['header', 'X-Session-Token'], 'uuid_parsing', GUEST_SESSION)], ['zz', 'UUID'])

    def test_t16_short_reset_token(self):
        response = self.client.post('/auth/reset-password', json={
            'password': PASSWORD, 'password_confirmation': PASSWORD, 'token': 'tok-marker-qzx'})
        self.assert_422(response, [(['body', 'token'], 'string_too_short', RESET_LINK_INVALID)], ['tok-marker-qzx'])

    def test_t16_other_fields_get_the_general_text(self):
        owner = {'headers': self.owner}
        cases = [
            ('guest_chart_id 0', '/auth/login', {'json': {'email': 'a@example.com', 'password': PASSWORD,
                                                          'guest_chart_id': 0}},
             [(['body', 'guest_chart_id'], 'greater_than', CHECK_INPUT)]),
            ('broken json', '/auth/login', {'content': '{bad', 'headers': {'content-type': 'application/json'}},
             [(['body', 1], 'json_invalid', CHECK_INPUT)]),
            ('list instead of object', '/auth/login', {'json': [1]},
             [(['body'], 'model_attributes_type', CHECK_INPUT)]),
            ('empty body', '/auth/login', {}, [(['body'], 'missing', CHECK_INPUT)]),
            ('blank question', '/ask-gpt', {'json': {'chart_id': 1, 'question': '   '}, **owner},
             [(['body', 'question'], 'string_pattern_mismatch', CHECK_INPUT)]),
            ('unknown field', '/relationships', {'json': {'chart_a_id': 1, 'chart_b_id': 2, 'person_a_label': 'A',
                                                          'person_b_label': 'B', 'zz_extra_field': 1}, **owner},
             [(['body', 'zz_extra_field'], 'extra_forbidden', CHECK_INPUT)]),
        ]
        for name, path, kwargs, expected in cases:
            with self.subTest(case=name):
                self.assert_422(self.client.post(path, **kwargs), expected, ['{bad'])

    def test_t16_own_validator_texts_lose_the_english_prefix(self):
        owner = {'headers': self.owner}
        same = self.client.post('/relationships', json={'chart_a_id': 1, 'chart_b_id': 1, 'person_a_label': 'A',
                                                        'person_b_label': 'B'}, **owner)
        self.assert_422(same, [(['body'], 'value_error', CHARTS_DIFFER)])
        label = self.client.post('/relationships', json={'chart_a_id': 1, 'chart_b_id': 2, 'person_a_label': '\x01',
                                                         'person_b_label': 'B'}, **owner)
        self.assert_422(label, [(['body', 'person_a_label'], 'value_error', LABEL_RULE)])
        moved = {**fixtures.PAYLOAD, 'selected_location': {'city': 'Another city', 'region': '', 'country': '',
                                                           'lat': 50.0, 'lon': 30.0, 'timezone': 'UTC'}}
        place = self.client.post('/natal-chart', json=moved, headers=self.guest)
        self.assert_422(place, [(['body'], 'value_error', LOCATION_CHANGED)])

    def test_t16_password_texts_stay(self):
        """Already Russian before this task; the new rules must not touch them."""
        missing = self.client.post('/auth/login', json={'email': 'a@example.com'})
        self.assert_422(missing, [(['body', 'password'], 'missing', ENTER_PASSWORD)])
        not_text = self.client.post('/auth/request-register', json={'email': 'q@example.com', 'password': 5})
        self.assert_422(not_text, [(['body', 'password'], 'password_policy', ENTER_PASSWORD)])
        differ = self.client.post('/auth/request-register', json={
            'email': 'q@example.com', 'password': PASSWORD, 'password_confirmation': 'x'})
        self.assert_422(differ, [(['body'], 'password_confirmation', PASSWORDS_DIFFER)])


if __name__ == '__main__':
    unittest.main()
