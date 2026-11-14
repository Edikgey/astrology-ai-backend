"""TRANSITS-1 guard and prompt regression TG1-TG7, TP1-TP2 (DESIGN §3, §4, §8).

Intentional contract change: test_ai_transit_guard.py::test_both_routes_stop_before_openai_and_quota_for_json_and_sse
and TRANSIT_UNAVAILABLE are replaced by TG4/TG5 - a transit question in a verified chart's chat is now answered.
The four refusal texts below are the DESIGN §4 literals. Clock: modules.transits.server_now frozen at NOW.
"""
import hashlib
import importlib.util
import inspect
import json
import unittest
from contextlib import nullcontext
from datetime import date, datetime, timedelta
from unittest.mock import patch

import test_relationship_ai as relationships
from test_ai_stream import ProviderStream
from test_ai_transit_guard import URANUS_QUESTION
from test_transits_accuracy import NOW
from test_transits_context import BIRTH_UTC, TransitContextBase, model_context, natal_bodies, sse_types
import test_chat_quality as cq
from database.queries import GPTMessage, GPTUsage, RelationshipMessage
from modules import ai_transit_guard, usage
from modules.ai_relationship_context import build_relationship_context
from modules.astrology_prompt import ASTROLOGY_SYSTEM_PROMPT
from modules.relationship_prompt import RELATIONSHIP_SYSTEM_PROMPT

LEGACY_TEXT = ('Для этой карты транзиты не считаются: она построена в старой версии Lunaria, где часовой пояс '
               'рождения не проверялся. Постройте карту заново, выбрав место рождения из подсказок, и спросите '
               'о транзитах в её чате.')
RELATIONSHIP_TEXT = ('В разборе пары транзиты не считаются. Спросите о транзитах в чате своей карты — там они '
                     'рассчитаны на 12 месяцев вперёд.')
BEYOND_TEXT = 'Транзиты рассчитаны с сегодняшнего дня по 14 ноября 2027 года. Спросите о периоде в эти сроки.'
NOT_CALCULATED_TEXT = ('Lunaria считает транзиты Юпитера, Сатурна, Урана, Нептуна и Плутона к натальной карте на '
                       '12 месяцев вперёд. Положения Солнца, Луны, Меркурия, Венеры и Марса на дату и переходы планет '
                       'в другой знак пока не считаются — спросите о транзитах к карте.')
OLD_FALSE_TEXT = 'пока не рассчитывает транзиты'

ANSWERED = (
    'Какие у меня сейчас транзиты?',
    'Покажи мои транзиты.',
    'Где сейчас Сатурн?',
    'Когда закончится мой транзит Плутона?',
    'Какие транзиты будут в 2027 году?',
    'Когда Сатурн войдёт в квадрат к моему Солнцу?',
    'Я родилась в 1990 году. Какие у меня сейчас транзиты?',
    'Где сейчас Марс и Сатурн?',
)
BEYOND = (
    'Какие транзиты будут в 2029 году?',
    'Покажи мои транзиты на 20 декабря 2027',
    'Какой транзит был 15.03.2025?',
    'Какие транзиты у меня через три года?',
)
NOT_CALCULATED = (
    'Где сейчас Луна?',
    'Покажи транзиты Марса.',
    'Когда Сатурн войдёт в Телец?',
)
# test_ai_transit_guard.test_keeps_natal_and_general_questions plus the natal Uranus question.
NATAL_AND_GENERAL = (
    'Где Уран в моей натальной карте?',
    'В каком знаке был Уран на момент рождения, 15 ноября 1996 года?',
    'Где в моей карте Юпитер? Сегодня мне предложили новую работу.',
    'Сегодня я прочитал про транзиты. Что такое транзиты?',
    'Что обычно означает транзит Сатурна через седьмой дом?',
    'Як Сатурн у моїй натальній карті впливає на стосунки?',
    'Where was Uranus at birth on November 15, 1996?',
    'What are transits in astrology?',
)
PROMPT_MUST_HAVE = ('transits', 'range_to', 'sky_now', 'Never predict events', '«около', 'Moon, Ascendant and Midheaven',
                    'legacy_chart', 'Not calculated', 'Never suggest a question the data cannot answer')
# TP2 baseline on 483235c: the pair prompt and the pair context limitations stay as they are.
RELATIONSHIP_PROMPT_SHA256 = 'cad5b142234d454f124240f1d09337c357590ead0fd1b0ca00f05b8789304a17'
RELATIONSHIP_LIMITATIONS = ['Saved natal/synastry data only; no transits or compatibility score.',
                            'Time provenance validates conversion, not the accuracy of reported birth time.',
                            'Unavailable angles/overlays must remain unknown.']


class GuardRouteBase(TransitContextBase):
    def post(self, url, payload, sse):
        headers = {**self.owner, **({'Accept': 'text/event-stream'} if sse else {})}
        return self.client.post(url, json=payload, headers=headers)

    def counts(self):
        with self.sessions() as db:
            return db.query(GPTUsage).count(), db.query(GPTMessage).count(), db.query(RelationshipMessage).count()

    def assert_refused(self, url, payload, detail):
        before = self.counts()
        with patch('modules.interpretation.client.chat.completions.create') as provider:
            for sse in (False, True):
                with self.subTest(question=payload['question'], sse=sse):
                    response = self.post(url, payload, sse)
                    self.assertEqual(response.status_code, 422, response.text)
                    self.assertEqual(response.json()['detail'], detail)
            provider.assert_not_called()
        self.assertEqual(self.counts(), before)

    def assert_answered_with_block(self, question):
        """A fresh verified chart per call: a long history would add a memory-summary provider call."""
        for sse in (False, True):
            cid = self.verified()
            with self.subTest(question=question, sse=sse):
                reply = ProviderStream() if sse else relationships.reply(answer='Ответ по транзитам')
                with patch('modules.interpretation.client.chat.completions.create', return_value=reply) as provider:
                    response = self.post('/ask-gpt', {'chart_id': cid, 'question': question}, sse)
                self.assertEqual(response.status_code, 200, response.text)
                if sse:
                    self.assertEqual(sse_types(response.text)[-1], 'done', response.text[-300:])
                provider.assert_called_once()
                block = model_context(provider.call_args)['transits']
                self.assertEqual(block['status'], 'ok')
                self.assertEqual(block['range_to'], '2027-11-14')


class TransitQuestionTests(GuardRouteBase):
    def test_tg1_transit_questions_in_a_verified_chart_reach_the_model_with_the_block(self):
        self.premium()
        for question in ANSWERED:
            self.assert_answered_with_block(question)

    def test_tg2_dates_outside_the_range_are_refused_with_the_range(self):
        cid = self.verified()
        for question in BEYOND:
            self.assert_refused('/ask-gpt', {'chart_id': cid, 'question': question}, BEYOND_TEXT)

    def test_tg3_bodies_and_ingresses_that_are_not_calculated_are_refused(self):
        cid = self.verified()
        for question in NOT_CALCULATED:
            self.assert_refused('/ask-gpt', {'chart_id': cid, 'question': question}, NOT_CALCULATED_TEXT)

    def test_tg4_legacy_chart_transit_question_is_refused_with_the_rebuild_hint(self):
        cid = self.legacy()
        self.assert_refused('/ask-gpt', {'chart_id': cid, 'question': URANUS_QUESTION}, LEGACY_TEXT)

    def test_tg4_same_question_in_a_verified_chart_is_answered(self):
        self.assert_answered_with_block(URANUS_QUESTION)

    def test_tg5_pair_chat_transit_question_is_refused_before_model_and_quota(self):
        relationship_id, _ = relationships.RelationshipAITests.pair(self)
        self.assert_refused(f'/relationships/{relationship_id}/ask', {'question': URANUS_QUESTION}, RELATIONSHIP_TEXT)

    def test_tg_texts_are_the_design_literals(self):
        self.assertEqual(ai_transit_guard.TRANSITS_LEGACY_CHART, LEGACY_TEXT)
        self.assertEqual(ai_transit_guard.TRANSITS_RELATIONSHIP, RELATIONSHIP_TEXT)
        self.assertEqual(ai_transit_guard.TRANSITS_NOT_CALCULATED, NOT_CALCULATED_TEXT)
        self.assertEqual(ai_transit_guard.transits_beyond_horizon(date(2027, 11, 14)), BEYOND_TEXT)
        self.assertEqual(ai_transit_guard.transits_beyond_horizon(date(2028, 3, 1)),
                         'Транзиты рассчитаны с сегодняшнего дня по 1 марта 2028 года. Спросите о периоде в эти сроки.')
        self.assertTrue(callable(ai_transit_guard.require_supported_chart_question))


class NatalQuestionsStillPass(cq.ChatQualityBase):
    """TG6 - green on base: natal and general questions reach the model for legacy and verified charts."""

    def test_tg6_natal_and_general_questions_pass_for_both_chart_kinds(self):
        frozen = (patch('modules.transits.server_now', return_value=NOW)
                  if importlib.util.find_spec('modules.transits') else nullcontext())
        with self.sessions() as db:
            now = datetime.utcnow()
            usage.set_plan(db, 1, 'premium', now - timedelta(days=1), now + timedelta(days=29))
        kinds = (('legacy', {}), ('verified', {'birth_utc': BIRTH_UTC, 'timezone': 'Europe/Kyiv'}))
        with frozen:
            for kind, fields in kinds:
                for question in NATAL_AND_GENERAL:
                    cid = self.chart()                    # fresh chart: no memory-summary call in between
                    self.store(cid, natal_bodies(), **fields)
                    with self.subTest(kind=kind, question=question), \
                            patch('modules.interpretation.client.chat.completions.create',
                                  return_value=relationships.reply(answer='Натальный ответ')) as provider:
                        response = self.client.post('/ask-gpt', json={'chart_id': cid, 'question': question},
                                                    headers=self.owner)
                        self.assertEqual(response.status_code, 200, response.text)
                        self.assertEqual(response.json()['response'], 'Натальный ответ')
                        provider.assert_called_once()


class GuardSourceTests(unittest.TestCase):
    def test_tg7_the_old_false_text_is_gone_from_the_guard(self):
        source = inspect.getsource(ai_transit_guard)
        self.assertNotIn(OLD_FALSE_TEXT, source)
        texts = [v for v in vars(ai_transit_guard).values() if isinstance(v, str)]
        self.assertFalse([t for t in texts if OLD_FALSE_TEXT in t])
        self.assertFalse(hasattr(ai_transit_guard, 'TRANSIT_UNAVAILABLE'))


class PromptTests(unittest.TestCase):
    def test_tp1_chart_prompt_describes_the_transit_block_and_its_limits(self):
        for phrase in PROMPT_MUST_HAVE:
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, ASTROLOGY_SYSTEM_PROMPT)
        self.assertNotIn('unless dated ephemeris data is supplied', ASTROLOGY_SYSTEM_PROMPT)
        flat = ' '.join(ASTROLOGY_SYSTEM_PROMPT.split())
        self.assertNotIn('unless dated ephemeris data is supplied', flat)

    def test_tp2_pair_prompt_and_pair_limitations_are_unchanged(self):
        """Green on base: no transits in the pair chat (DECISIONS №26)."""
        self.assertEqual(hashlib.sha256(RELATIONSHIP_SYSTEM_PROMPT.encode('utf-8')).hexdigest(),
                         RELATIONSHIP_PROMPT_SHA256)


class PairLimitationsTests(cq.ChatQualityBase):
    def test_tp2_pair_context_limitations_are_unchanged(self):
        """Green on base."""
        relationship_id, _ = relationships.RelationshipAITests.pair(self)
        with self.sessions() as db:
            context = json.loads(build_relationship_context(db, 1, relationship_id))
        self.assertEqual(context['limitations'], RELATIONSHIP_LIMITATIONS)
        self.assertNotIn('transits', {k for k in context if k != 'limitations'})


if __name__ == '__main__':
    unittest.main()
