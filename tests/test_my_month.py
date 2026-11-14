"""MY-MONTH-be regression MG1-MG5, MC1-MC10, MP1-MP3 (tasks/MY-MONTH/DESIGN.md section 10; texts 2.3, 3.4, 6).

Written before the code. New names (is_month_request, MY_MONTH_*, MY_MONTH_PROMPT, the `month` argument) are
imported or used inside the tests, so on the base every test fails for its own reason and not on module import.
Guards that are green on the base: MG4, MG5, MG6, MC2, MC7 (ordinary question part), MP2.
Clock: modules.transits.server_now frozen at NOW (2026-11-14 02:15 UTC, Kyiv day 2026-11-14). Synthetic data
only, no network, no .secrets, no environment dumps; OpenAI is a stub.
"""
import hashlib
import json
import re
import unittest
from contextlib import contextmanager
from datetime import timedelta
from itertools import combinations
from unittest.mock import patch

import swisseph

import test_chat_quality as cq
import test_relationship_ai as relationships
from test_ai_stream import ProviderStream
from test_transits_accuracy import NOW, POINTS
from test_transits_context import (BIRTH_UTC, CANARY, FAILED_LINE, UNAVAILABLE_LEGACY, TransitContextBase, captured_logs,
                                   compact, model_context, natal_bodies, sse_types, transit_lines, utc)
from test_transits_guard import GuardRouteBase, LEGACY_TEXT as TRANSITS_LEGACY_TEXT, \
    RELATIONSHIP_TEXT as TRANSITS_RELATIONSHIP_TEXT
from database.queries import GPTUsage
from modules import ai_chart_context
from modules.ai_chart_context import build_chart_context
from modules.astrology_prompt import ASTROLOGY_SYSTEM_PROMPT, SUMMARY_SYSTEM_PROMPT
from modules.relationship_prompt import RELATIONSHIP_SYSTEM_PROMPT

# DESIGN 2.3 and 3.4 literals.
MONTH_QUESTION = 'Мой месяц: что меня ждёт в ближайшие 30 дней?'
WRITTEN_MONTH_QUESTION = 'Что меня ждёт в этом месяце?'
TRANSIT_MONTH_QUESTION = 'Какие транзиты ждут меня в этом месяце?'     # flagged by TRANSITS-1 and about the month
MY_MONTH_LEGACY_CHART = (
    'Обзор месяца строится по транзитам, а для этой карты они не считаются: она построена в старой версии '
    'Lunaria, где часовой пояс рождения не проверялся. Постройте карту заново, выбрав место рождения из '
    'подсказок, и спросите о своём месяце в её чате.')
MY_MONTH_RELATIONSHIP = ('В разборе пары обзор месяца не строится — здесь нет транзитов. '
                         'Спросите «Мой месяц» в чате своей карты.')
MY_MONTH_UNAVAILABLE = ('Сейчас не получилось рассчитать транзиты для обзора месяца. Квота не списана — '
                        'попробуйте ещё раз через несколько минут.')

# DESIGN 2.4: the contract of the detector.
IS_MONTH = (
    'Мой месяц: что меня ждёт в ближайшие 30 дней?', 'Мой месяц', 'мой месяц?', 'Расскажи о моём месяце',
    'Что меня ждёт в этом месяце?', 'Что ждёт меня в ближайший месяц?', 'Сделай прогноз на этот месяц',
    'Прогноз на месяц', 'Обзор месяца', 'Гороскоп на месяц', 'Обзор на ближайшие 30 дней',
    'Что мне ждать в текущем месяце?', 'Чего ожидать в этом месяце в работе?', 'Что меня ждёт на месяц вперёд?',
    'На что обратить внимание в этом месяце?', 'Що мене чекає цього місяця?', 'Мій місяць',
    'What can I expect this month?', 'My month', 'What does the month ahead hold?',
    'Какие транзиты ждут меня в этом месяце?', 'Что меня ждёт в этом месяце? Я родилась 3 мая.',
)
NOT_MONTH = (
    'Что меня ждёт?', 'Что меня ждёт в следующем месяце?', 'Что было в прошлом месяце?',
    'В каком месяце я родилась?', 'Мой месяц рождения — март', 'В этом месяце мне грустно',
    'Что меня ждёт в декабре?', 'Какие транзиты в этом месяце?', 'Что меня ждёт в ближайшие месяцы?',
    'Покажи мои транзиты на этот месяц', 'Что меня ждёт в этом году?', 'Где сейчас Юпитер?',
    'Що мене чекає в наступному місяці?', 'What will next month bring?',
    'Что меня ждёт в этом месяце до Рождества?',
)
NOT_ABOUT_THE_MONTH = ('Что меня ждёт?', 'В каком месяце я родилась?', 'В этом месяце мне грустно, почему?')
MONTH_ASKS = (MONTH_QUESTION, WRITTEN_MONTH_QUESTION)

PROMPT_FRAGMENTS = (
    'transits.month', 'month.from', 'month.to', 'month.omitted', '**Любовь и отношения**', '**Работа и деньги**',
    '**Силы и самочувствие**', '**На что обратить внимание**', '«около 12 декабря»',
    'Quote only dates from month rows', 'never medical advice', 'If month.rows is empty',
    'replace the 1-3 bold ideas rule')

MONTH_TRIMMED_LINE = re.compile(r'^AI month outcome=trimmed rows=(\d+) omitted=(\d+)$')
MONTH_COLUMNS_COUNT = 6

# Baselines of the base commit c31feeb (pinned before any code exists).
ASTROLOGY_PROMPT_SHA256 = 'b60317091e20f2d06a53b01daa4a6c1257593382955e551a390b398c6f831d1d'
SUMMARY_PROMPT_SHA256 = '61daaaaaca1b4bfd6ee12b8661a7b95cd4f320664f84d0877230950667b04d89'
RELATIONSHIP_PROMPT_SHA256 = 'cad5b142234d454f124240f1d09337c357590ead0fd1b0ca00f05b8789304a17'
ORDINARY_CONTEXT_SHA256 = '362a2b85c08fdadafc45be9346a3cebdef39a8cc424a32ee0638c5e45a8839bc'


def sse_events(text):
    return [json.loads(chunk[len('data: '):]) for chunk in text.split('\n\n') if chunk.startswith('data: ')]


class MonthBase(GuardRouteBase):
    """A verified or legacy chart with a premium owner (no daily limit in the way)."""

    def setUp(self):
        super().setUp()
        self.premium()

    def synthetic_window(self):
        """DESIGN 4.2 selection cases, Kyiv, today 2026-11-14, window to 2026-12-14 (inclusive)."""
        t = self.synthetic
        return [
            # R1: starts on the last day of the window -> in; its exact date is after the window -> dropped
            t('♃', '☌', '☉', [(utc(2026, 12, 14, 12), utc(2027, 1, 20, 12))], [utc(2026, 12, 20, 12)]),
            # R2: starts the day after the window -> out
            t('♄', '☌', '☽', [(utc(2026, 12, 15, 12), utc(2027, 1, 20, 12))], []),
            # R3: two periods; only the one that meets the window stays
            t('♅', '□', '☿', [(utc(2026, 11, 1, 12), utc(2026, 11, 20, 12)), (utc(2027, 6, 1, 12), utc(2027, 6, 20, 12))],
              [utc(2026, 11, 15, 12), utc(2027, 6, 10, 12)]),
            # R4: began before the search (None) -> in, None stays None
            t('♆', '△', '♀', [(None, utc(2026, 12, 1, 12))], []),
            # R5: began long before the window; exact dates are cut to the window by the LOCAL day
            t('♇', '☍', '♂', [(utc(2026, 10, 1, 12), utc(2027, 2, 1, 12))],
              [utc(2026, 11, 13, 21, 30), utc(2026, 11, 13, 22, 30), utc(2026, 11, 20, 12), utc(2026, 12, 14, 21, 30),
               utc(2026, 12, 14, 22, 30), utc(2027, 1, 15, 12)], orb_now=0.3),
            # R6: open end -> in, None stays None
            t('♃', '⚹', 'AS', [(utc(2026, 12, 10, 12), None)], []),
            # R7: 22:30 UTC on 13 Dec is 14 Dec in Kyiv -> in
            t('♄', '⚹', 'MC', [(utc(2026, 12, 13, 22, 30), utc(2027, 1, 5, 12))], []),
            # R8: 22:30 UTC on 14 Dec is 15 Dec in Kyiv -> out
            t('♅', '△', '☉', [(utc(2026, 12, 14, 22, 30), utc(2027, 1, 5, 12))], []),
        ]

    EXPECTED_MONTH_ROWS = {
        ('Юпитер', 'соединение', 'Солнце'): ['Юпитер', 'соединение', 'Солнце', None, [['2026-12-14', '2027-01-20']], []],
        ('Уран', 'квадрат', 'Меркурий'): ['Уран', 'квадрат', 'Меркурий', None, [['2026-11-01', '2026-11-20']], ['2026-11-15']],
        ('Нептун', 'трин', 'Венера'): ['Нептун', 'трин', 'Венера', None, [[None, '2026-12-01']], []],
        ('Плутон', 'оппозиция', 'Марс'): ['Плутон', 'оппозиция', 'Марс', 0.3, [['2026-10-01', '2027-02-01']],
                                         ['2026-11-14', '2026-11-20', '2026-12-14']],
        ('Юпитер', 'секстиль', 'Асцендент'): ['Юпитер', 'секстиль', 'Асцендент', None, [['2026-12-10', None]], []],
        ('Сатурн', 'секстиль', 'Средина Неба'): ['Сатурн', 'секстиль', 'Средина Неба', None,
                                                [['2026-12-14', '2027-01-05']], []],
    }
    OUT_OF_WINDOW = (('Сатурн', 'соединение', 'Луна'), ('Уран', 'трин', 'Солнце'))

    def ask(self, cid, question, sse=False, reply=None):
        reply = reply or (ProviderStream() if sse else relationships.reply(answer='Ответ по месяцу'))
        with patch('modules.interpretation.client.chat.completions.create', return_value=reply) as provider:
            response = self.post('/ask-gpt', {'chart_id': cid, 'question': question}, sse)
        return response, provider

    def month_context(self, cid, **kwargs):
        with self.sessions() as db:
            return json.loads(build_chart_context(db, 1, cid, month=True, **kwargs))

    def plain_context(self, cid):
        with self.sessions() as db:
            return json.loads(build_chart_context(db, 1, cid))

    def reserved_rows(self):
        with self.sessions() as db:
            return db.query(GPTUsage).filter_by(status='reserved').count()

    def big_verified_chart(self):
        order = list(cq.NAMES)
        symbols = list(cq.ASPECT_NAMES)
        circle, lines = [], []
        for i, (a, b) in enumerate(list(combinations(order, 2))[:250]):
            circle.append(cq.edge(a, b, symbols[i % 13]))
            lines.append(cq.line(symbols[i % 13], a, b, (i * 7) % 240))
        cid = self.chart()
        self.store(cid, cq.spread(order), circle, cq.structured(lines), birth_utc=BIRTH_UTC, timezone='Europe/Kyiv')
        return cid

    def many_in_window(self, count):
        bodies, aspects = ['♃', '♄', '♅', '♆', '♇'], ['☌', '⚹', '□', '△', '☍']
        rows = []
        for i in range(count):
            begin = utc(2026, 11, 16, 12) + timedelta(hours=7 * i)
            rows.append(self.synthetic(bodies[i % 5], aspects[(i // 5) % 5], POINTS[i % 12],
                                       [(begin, begin + timedelta(days=9))],
                                       [begin + timedelta(days=1), begin + timedelta(days=3), begin + timedelta(days=5)]))
        return rows


# --------------------------------------------------------------------------------------------- MG: detector, refusals

class DetectorTests(unittest.TestCase):
    def test_mg1_the_detector_follows_the_design_lists(self):
        from modules.ai_transit_guard import is_month_request
        self.assertEqual((len(IS_MONTH), len(NOT_MONTH)), (22, 15))
        for question in IS_MONTH:
            with self.subTest(month=question):
                self.assertIs(is_month_request(question), True)
        for question in NOT_MONTH:
            with self.subTest(not_month=question):
                self.assertIs(is_month_request(question), False)

    def test_mg1_the_button_text_is_not_a_transit_question_for_the_old_guard(self):
        """The button text and the written variant stay outside TRANSITS-1 (no refusal of their own)."""
        from modules.ai_transit_guard import is_month_request, requires_transit_data
        self.assertTrue(is_month_request(MONTH_QUESTION))
        self.assertFalse(requires_transit_data(MONTH_QUESTION))
        self.assertFalse(requires_transit_data(WRITTEN_MONTH_QUESTION))
        self.assertTrue(requires_transit_data(TRANSIT_MONTH_QUESTION))        # why MG4 exists

    def test_mg1_texts_are_the_design_literals(self):
        from modules import ai_transit_guard
        self.assertEqual(ai_transit_guard.MY_MONTH_LEGACY_CHART, MY_MONTH_LEGACY_CHART)
        self.assertEqual(ai_transit_guard.MY_MONTH_RELATIONSHIP, MY_MONTH_RELATIONSHIP)
        self.assertEqual(ai_transit_guard.MY_MONTH_UNAVAILABLE, MY_MONTH_UNAVAILABLE)


class RefusalTests(MonthBase):
    def test_mg2_legacy_chart_month_question_is_refused_before_model_and_quota(self):
        cid = self.legacy()
        for question in MONTH_ASKS:
            self.assert_refused('/ask-gpt', {'chart_id': cid, 'question': question}, MY_MONTH_LEGACY_CHART)

    def test_mg3_pair_chat_month_question_is_refused_before_model_and_quota(self):
        relationship_id, _ = relationships.RelationshipAITests.pair(self)
        for question in MONTH_ASKS:
            self.assert_refused(f'/relationships/{relationship_id}/ask', {'question': question}, MY_MONTH_RELATIONSHIP)

    def test_mg4_transit_question_about_the_month_keeps_the_transits_refusal(self):
        """Green guard: TRANSITS-1 decides first for a question it flags (legacy chart, pair chat)."""
        cid = self.legacy()
        self.assert_refused('/ask-gpt', {'chart_id': cid, 'question': TRANSIT_MONTH_QUESTION}, TRANSITS_LEGACY_TEXT)
        relationship_id, _ = relationships.RelationshipAITests.pair(self)
        self.assert_refused(f'/relationships/{relationship_id}/ask', {'question': TRANSIT_MONTH_QUESTION},
                            TRANSITS_RELATIONSHIP_TEXT)

    def test_mg5_questions_not_about_the_month_are_still_answered_for_legacy_and_pair(self):
        """Green guard: no false refusal for the neighbours of the month question."""
        for question in NOT_ABOUT_THE_MONTH:
            for sse in (False, True):
                reply = ProviderStream() if sse else relationships.reply(answer='Обычный ответ')
                cid = self.legacy()
                with self.subTest(kind='legacy', question=question, sse=sse), patch(
                        'modules.interpretation.client.chat.completions.create', return_value=reply) as provider:
                    response = self.post('/ask-gpt', {'chart_id': cid, 'question': question}, sse)
                    self.assertEqual(response.status_code, 200, response.text)
                    provider.assert_called_once()
                relationship_id, _ = relationships.RelationshipAITests.pair(self)
                reply = ProviderStream() if sse else relationships.reply(answer='Обычный ответ пары')
                with self.subTest(kind='pair', question=question, sse=sse), patch(
                        'modules.interpretation.client.chat.completions.create', return_value=reply) as provider:
                    response = self.post(f'/relationships/{relationship_id}/ask', {'question': question}, sse)
                    self.assertEqual(response.status_code, 200, response.text)
                    provider.assert_called_once()

    def test_mg6_ownership_is_checked_before_the_month_refusal(self):
        """Green guard (contour 1): someone else's chart gets the ownership error, not the legacy text."""
        cid = self.legacy()
        with patch('modules.interpretation.client.chat.completions.create') as provider:
            response = self.client.post('/ask-gpt', json={'chart_id': cid, 'question': MONTH_QUESTION},
                                        headers=self.other)
        self.assertEqual(response.status_code, 403, response.text)
        self.assertNotEqual(response.json()['detail'], MY_MONTH_LEGACY_CHART)
        provider.assert_not_called()


# --------------------------------------------------------------------------------------------- MC: the month slice

class MonthSliceTests(MonthBase):
    def run_route(self, question, sse, rows=None):
        cid = self.verified()
        rows = self.synthetic_window() if rows is None else rows
        with self.patch_find(return_value=rows):
            finder = self.transits.find_transits
            response, provider = self.ask(cid, question, sse)
        self.assertEqual(response.status_code, 200, response.text)
        if sse:
            self.assertEqual(sse_types(response.text)[-1], 'done', response.text[-300:])
        provider.assert_called_once()
        return model_context(provider.call_args), finder

    def test_mc1_month_questions_reach_the_model_with_the_month_slice_and_one_calculation(self):
        for question in (MONTH_QUESTION, WRITTEN_MONTH_QUESTION, TRANSIT_MONTH_QUESTION):
            for sse in (False, True):
                with self.subTest(question=question, sse=sse):
                    ctx, finder = self.run_route(question, sse)
                    block = ctx['transits']
                    self.assertEqual(block['status'], 'ok')
                    self.assertEqual((block['month']['from'], block['month']['to']), ('2026-11-14', '2026-12-14'))
                    self.assertEqual(finder.call_count, 1)
                    plain, _ = self.run_route('Что меня ждёт в работе?', sse)
                    self.assertNotIn('month', plain['transits'])
                    self.assertEqual({k: v for k, v in block.items() if k != 'month'}, plain['transits'])
                    self.assertEqual({k: v for k, v in ctx.items() if k != 'transits'},
                                     {k: v for k, v in plain.items() if k != 'transits'})

    def test_mc2_ordinary_question_context_is_byte_for_byte_the_base(self):
        """Green guard: SHA of the base context with a pinned set of transit rows; no `month` key."""
        cid = self.verified()
        with self.patch_find(return_value=self.synthetic_window()):
            text = json.dumps(self.plain_context(cid), ensure_ascii=False, separators=(',', ':'), allow_nan=False)
            with self.sessions() as db:
                raw = build_chart_context(db, 1, cid)
            response, provider = self.ask(cid, 'Что меня ждёт в работе?')
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn('month', json.loads(raw)['transits'])
        self.assertEqual(hashlib.sha256(raw.encode('utf-8')).hexdigest(), ORDINARY_CONTEXT_SHA256)
        self.assertEqual(text, raw)
        sent = provider.call_args.kwargs['messages'][1]['content']
        self.assertEqual(sent.split('\n', 1)[1], raw)

    def test_mc3_selection_by_local_day_and_cut_exact_dates(self):
        cid = self.verified()
        with self.patch_find(return_value=self.synthetic_window()):
            ctx = self.month_context(cid)
        block = ctx['transits']
        month = block['month']
        self.assertEqual((month['from'], month['to'], month['omitted']), ('2026-11-14', '2026-12-14', 0))
        got = {tuple(row[:3]): row for row in month['rows']}
        self.assertEqual(len(got), len(month['rows']))
        for ident, row in self.EXPECTED_MONTH_ROWS.items():
            with self.subTest(row=ident):
                self.assertEqual(got.get(ident), row)
        for ident in self.OUT_OF_WINDOW:
            with self.subTest(out=ident):
                self.assertNotIn(ident, got)
        self.assertEqual(set(got), set(self.EXPECTED_MONTH_ROWS))
        # the year block keeps everything, including the far dates the month slice cuts
        year = {tuple(row[:3]): row for row in block['rows']}
        self.assertEqual(len(year), 8)
        self.assertEqual(year[('Плутон', 'оппозиция', 'Марс')][5],
                         ['2026-11-13', '2026-11-14', '2026-11-20', '2026-12-14', '2026-12-15', '2027-01-15'])
        self.assertEqual(year[('Уран', 'квадрат', 'Меркурий')][4],
                         [['2026-11-01', '2026-11-20'], ['2027-06-01', '2027-06-20']])
        self.assertEqual(year[('Юпитер', 'соединение', 'Солнце')][5], ['2026-12-20'])

    def test_mc4_shape_order_and_position_of_the_month_key(self):
        for bodies in (None, natal_bodies(skip=('AS', 'MC'))):
            cid = self.verified(bodies=bodies)
            with self.subTest(points_missing=bodies is not None), self.patch_find(return_value=self.synthetic_window()):
                ctx = self.month_context(cid)
                block = ctx['transits']
                self.assertEqual(list(ctx)[-1], 'transits')
                self.assertEqual(list(block)[-1], 'month')
                if bodies is not None:
                    self.assertLess(list(block).index('points_missing'), list(block).index('month'))
                self.assertEqual(list(block['month']), ['from', 'to', 'rows', 'omitted'])
                self.assertTrue(block['month']['rows'])
                for row in block['month']['rows']:
                    self.assertEqual(len(row), MONTH_COLUMNS_COUNT)
                self.assertEqual(block['columns'], ['body', 'aspect', 'point', 'orb_now', 'periods', 'exact'])
                ids = [tuple(row[:3]) for row in block['month']['rows']]
                in_year_order = [tuple(row[:3]) for row in block['rows'] if tuple(row[:3]) in set(ids)]
                self.assertEqual(ids, in_year_order)

    def test_mc5_month_budget_trims_from_the_tail_and_logs_one_closed_line(self):
        self.assertEqual(getattr(ai_chart_context, 'MONTH_BLOCK_BUDGET', None), 2000)
        self.assertEqual(getattr(ai_chart_context, 'MONTH_DAYS', None), 30)
        cid = self.verified()
        rows = self.many_in_window(40)
        with self.patch_find(return_value=rows):
            ordinary = self.plain_context(cid)['transits']
            with captured_logs() as lines:
                ctx = self.month_context(cid)
            with patch.object(ai_chart_context, 'MONTH_BLOCK_BUDGET', 10 ** 6), captured_logs() as quiet:
                full = self.month_context(cid)['transits']['month']
        month = ctx['transits']['month']
        self.assertLessEqual(len(compact(month).encode('utf-8')), 2000)
        self.assertGreater(month['omitted'], 0)
        self.assertEqual(full['omitted'], 0)
        self.assertEqual(len(full['rows']), 40)
        self.assertEqual(len(month['rows']) + month['omitted'], 40)
        self.assertEqual(month['rows'], full['rows'][:len(month['rows'])])
        one_more = {**month, 'rows': full['rows'][:len(month['rows']) + 1], 'omitted': month['omitted'] - 1}
        self.assertGreater(len(compact(one_more).encode('utf-8')), 2000)          # the largest head that fits
        found = [line for line in lines if line.startswith('AI month')]
        self.assertEqual(len(found), 1, found)
        match = MONTH_TRIMMED_LINE.match(found[0])
        self.assertTrue(match, found[0])
        self.assertEqual((int(match[1]), int(match[2])), (len(month['rows']), month['omitted']))
        self.assertEqual([line for line in quiet if line.startswith('AI month')], [])
        self.assertEqual({k: v for k, v in ctx['transits'].items() if k != 'month'}, ordinary)   # year block untouched

    def test_mc5_no_trim_no_log_line(self):
        cid = self.verified()
        with self.patch_find(return_value=self.synthetic_window()), captured_logs() as lines:
            ctx = self.month_context(cid)
        self.assertEqual(ctx['transits']['month']['omitted'], 0)
        self.assertEqual([line for line in lines if line.startswith('AI month')], [])

    def test_mc6_big_chart_in_month_mode_fits_and_keeps_placements_cusps_and_year_rows(self):
        cid = self.big_verified_chart()
        rows = self.many_in_window(40)
        with self.patch_find(return_value=rows):
            ordinary = self.plain_context(cid)
            with self.sessions() as db:
                text = build_chart_context(db, 1, cid, month=True)
        ctx = json.loads(text)
        self.assertLessEqual(len(text.encode('utf-8')), 24000)
        self.assertEqual(ctx['transits']['status'], 'ok')
        self.assertTrue(ctx['transits']['month']['rows'])
        self.assertEqual(len(ctx['placements']), 26)
        self.assertEqual(len(ctx['houses']['cusps']), 12)
        self.assertEqual(ctx['transits']['rows'], ordinary['transits']['rows'])
        self.assertGreaterEqual(ctx['omitted']['aspects'], ordinary['omitted']['aspects'])
        self.assertEqual(len(ctx['aspects']) + ctx['omitted']['aspects'],
                         len(ordinary['aspects']) + ordinary['omitted']['aspects'])

    def test_mc7_calculation_failure_in_month_mode_is_503_with_the_quota_released(self):
        for error in (RuntimeError(CANARY), swisseph.Error('ephemeris file missing')):
            for sse in (False, True):
                cid = self.verified()
                before, held = self.used(), self.reserved_rows()
                with self.subTest(error=type(error).__name__, sse=sse), captured_logs() as lines, \
                        self.patch_find(side_effect=error):
                    response, provider = self.ask(cid, MONTH_QUESTION, sse)
                    if sse:
                        events = sse_events(response.text)
                        self.assertEqual(events[-1]['type'], 'error', response.text[-300:])
                        self.assertEqual((events[-1]['status'], events[-1]['detail']), (503, MY_MONTH_UNAVAILABLE))
                    else:
                        self.assertEqual(response.status_code, 503, response.text)
                        self.assertEqual(response.json()['detail'], MY_MONTH_UNAVAILABLE)
                    provider.assert_not_called()                                  # neither the answer nor a summary
                    self.assertEqual((self.used(), self.reserved_rows()), (before, held))
                    found = transit_lines(lines)
                    self.assertEqual(len(found), 1, found)
                    self.assertRegex(found[0], FAILED_LINE)
                    for leak in ('КАНАРЕЙКА', '7781', '1990-05-03', 'ephemeris file missing'):
                        self.assertNotIn(leak, response.text)
                        for line in lines:
                            self.assertNotIn(leak, line)

    def test_mc7_ordinary_question_with_the_same_failure_is_still_answered(self):
        """Green guard (base behaviour): the failure only blocks the month overview."""
        for sse in (False, True):
            cid = self.verified()
            with self.subTest(sse=sse), self.patch_find(side_effect=RuntimeError(CANARY)):
                response, provider = self.ask(cid, 'Что меня ждёт в работе?', sse)
                self.assertEqual(response.status_code, 200, response.text)
                provider.assert_called_once()
                self.assertEqual(model_context(provider.call_args)['transits'],
                                 {'status': 'unavailable', 'reason': 'calculation_failed'})

    def test_mc8_empty_window_is_an_answer_with_empty_rows(self):
        far = [self.synthetic('♃', '☌', '☉', [(utc(2027, 2, 1, 12), utc(2027, 3, 1, 12))], [utc(2027, 2, 15, 12)])]
        for rows in ([], far):
            for sse in (False, True):
                with self.subTest(rows=len(rows), sse=sse):
                    ctx, _ = MonthSliceTests.run_route(self, MONTH_QUESTION, sse, rows=rows)
                    month = ctx['transits']['month']
                    self.assertEqual((month['rows'], month['omitted']), ([], 0))
                    self.assertEqual((month['from'], month['to']), ('2026-11-14', '2026-12-14'))

    def test_mc9_one_clock_per_request_local_day_and_a_stable_context(self):
        cid = self.verified(timezone='America/Los_Angeles')
        with patch.object(self.transits, 'server_now', return_value=NOW) as clock, \
                self.patch_find(return_value=self.synthetic_window()):
            response, provider = self.ask(cid, MONTH_QUESTION)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(clock.call_count, 1)
            first = provider.call_args.kwargs['messages'][1]['content']
            response, provider = self.ask(cid, MONTH_QUESTION)
            self.assertEqual(response.status_code, 200, response.text)
            second = provider.call_args.kwargs['messages'][1]['content']
        month = json.loads(first.split('\n', 1)[1])['transits']['month']
        self.assertEqual((month['from'], month['to']), ('2026-11-13', '2026-12-13'))
        self.assertEqual(first, second)

    def test_mc10_context_builder_refuses_a_legacy_chart_in_month_mode(self):
        from fastapi import HTTPException
        cid = self.legacy()
        with self.sessions() as db, self.assertRaises(HTTPException) as caught:
            build_chart_context(db, 1, cid, month=True)
        self.assertEqual((caught.exception.status_code, caught.exception.detail), (422, MY_MONTH_LEGACY_CHART))
        self.assertEqual(self.context(cid)['transits'], UNAVAILABLE_LEGACY)       # the plain builder still answers

    def test_mc10_context_builder_gives_503_when_the_calculation_fails_in_month_mode(self):
        from fastapi import HTTPException
        cid = self.verified()
        with self.patch_find(side_effect=RuntimeError(CANARY)), self.sessions() as db, \
                self.assertRaises(HTTPException) as caught:
            build_chart_context(db, 1, cid, month=True)
        self.assertEqual((caught.exception.status_code, caught.exception.detail), (503, MY_MONTH_UNAVAILABLE))


# --------------------------------------------------------------------------------------------- MP: prompt

class PromptTests(MonthBase):
    def test_mp1_month_prompt_has_the_pinned_fragments(self):
        from modules.astrology_prompt import MY_MONTH_PROMPT
        self.assertIsInstance(MY_MONTH_PROMPT, str)
        self.assertEqual(len(PROMPT_FRAGMENTS), 13)
        flat = ' '.join(MY_MONTH_PROMPT.split())
        for fragment in PROMPT_FRAGMENTS:
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, flat)

    def test_mp2_base_prompts_are_unchanged(self):
        """Green guard: the base prompts are pinned byte for byte (DECISIONS №21, №22, №26)."""
        sha = lambda text: hashlib.sha256(text.encode('utf-8')).hexdigest()
        self.assertEqual(sha(ASTROLOGY_SYSTEM_PROMPT), ASTROLOGY_PROMPT_SHA256)
        self.assertEqual(sha(SUMMARY_SYSTEM_PROMPT), SUMMARY_PROMPT_SHA256)
        self.assertEqual(sha(RELATIONSHIP_SYSTEM_PROMPT), RELATIONSHIP_PROMPT_SHA256)

    def test_mp3_month_request_gets_base_plus_month_prompt_and_the_same_call_parameters(self):
        from modules.astrology_prompt import MY_MONTH_PROMPT
        for sse in (False, True):
            with self.subTest(sse=sse), self.patch_find(return_value=self.synthetic_window()):
                ordinary_chart, month_chart = self.verified(), self.verified()
                response, ordinary = self.ask(ordinary_chart, 'Что меня ждёт в работе?', sse)
                self.assertEqual(response.status_code, 200, response.text)
                response, month = self.ask(month_chart, MONTH_QUESTION, sse)
                self.assertEqual(response.status_code, 200, response.text)
                ordinary_call, month_call = ordinary.call_args.kwargs, month.call_args.kwargs
                self.assertEqual(ordinary_call['messages'][0], {'role': 'system', 'content': ASTROLOGY_SYSTEM_PROMPT})
                self.assertEqual(month_call['messages'][0],
                                 {'role': 'system', 'content': ASTROLOGY_SYSTEM_PROMPT + '\n' + MY_MONTH_PROMPT})
                rest = lambda kwargs: {k: v for k, v in kwargs.items() if k != 'messages'}
                self.assertEqual(rest(month_call), rest(ordinary_call))
                self.assertEqual(month_call['messages'][1]['role'], 'user')


if __name__ == '__main__':
    unittest.main()
