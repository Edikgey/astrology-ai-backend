"""CHAT-QUALITY-be regression (CQ-1 ... CQ-11): what the AI chat sends to the model and how it behaves when
the model cuts off.  Real context/accounting code, fake provider, isolated SQLite, synthetic data only.
No OpenAI call is ever made; every provider call goes through a patched ``client.chat.completions.create``.

Contract source: tasks/CHAT-QUALITY/DESIGN.md sections 3, 6 and 7.
"""
import json
import logging
import os
import unittest
from datetime import datetime, timedelta
from itertools import combinations
from unittest.mock import patch

import swisseph
from fastapi import HTTPException
from openai import NOT_GIVEN

import test_my_charts as fixtures
import test_ai_conversation as conv
from test_ai_stream import ProviderStream
from test_synastry import facts
from database.queries import ChartData, GPTConversation, GPTMessage, GPTUsage, NatalChart, Relationship, User
from modules import usage, ai_chart_context
from modules.ai_chart_context import build_chart_context
from modules.ai_relationship_context import build_relationship_context
from modules.ai_stream import chat_events
from modules.aspects import Aspects
from modules.ephemeris import Ephemeris
from modules.interpretation import client, OPENAI_PARAMS
from modules.synastry import build_synastry_snapshot

reply = conv.reply

SIGNS = ['Овен', 'Телец', 'Близнецы', 'Рак', 'Лев', 'Дева', 'Весы', 'Скорпион', 'Стрелец', 'Козерог', 'Водолей', 'Рыбы']
NAMES = {
    '☉': 'Солнце', '☽': 'Луна', '☿': 'Меркурий', '♀': 'Венера', '♂': 'Марс', '♃': 'Юпитер', '♄': 'Сатурн',
    '♅': 'Уран', '♆': 'Нептун', '♇': 'Плутон', '☊': 'Северный Узел', '☋': 'Южный Узел', '⚳': 'Церера',
    '⚴': 'Паллада', '⚵': 'Юнона', '⚶': 'Веста', '⚷': 'Хирон', 'φ': 'Фол', '⚸': 'Черная Луна Лилит',
    'W': 'Белая Луна Селена', 'AS': 'Асцендент', 'DS': 'Десцендент', 'MC': 'Средина Неба', 'IC': 'Надир',
    'Vx': 'Вертекс', 'X': 'Часть Фортуны',
}
ASPECT_NAMES = {'☌': 'соединение', '☍': 'оппозиция', '△': 'трин', '□': 'квадрат', '⚹': 'секстиль',
                '⚻': 'квинконс', '∠': 'полуквадрат', 'bQ': 'биквинтиль', 'N': 'новиль', 'S': 'септиль',
                '⚺': 'полусекстиль', 'Q': 'квинтиль', '∠∠': 'полутороквадрат'}
MAJOR = {'☌', '☍', '△', '□', '⚹'}
RAW_GLYPHS = {'W', 'X', 'S', 'N', 'Q', 'bQ', '∠', '∠∠', 'AS', 'DS', 'MC', 'IC', 'Vx'} | set(ASPECT_NAMES) | set(NAMES)

TRUNCATED = ('Ответ получился слишком длинным и оборвался. Квота не списана. '
             'Попросите ответить короче или задайте вопрос по частям.')
FILTERED = 'AI не смог ответить на этот вопрос. Квота не списана. Попробуйте сформулировать его иначе.'
NETWORK_CUT = 'Ответ прервался. Обновите историю перед повторной отправкой.'


def body(symbol, longitude, house=1, retrograde=False):
    # roundedDegree is deliberately wrong: the context must not use the rounded (30 deg) display field.
    return {'symbol': symbol, 'label': NAMES[symbol], 'degree': longitude, 'roundedDegree': 'WRONG',
            'sign': SIGNS[int(longitude // 30)], 'house': house, 'retrograde': retrograde}


def spread(symbols):
    return {s: body(s, (i * 13.7 + 3.3) % 360, i % 12 + 1) for i, s in enumerate(symbols)}


def cusps(first=11.634):
    # Twelve cusps, each at 11 deg 38 min of consecutive signs.
    return [{'symbol': str(i + 1), 'degree': i * 30 + first} for i in range(12)]


def line(symbol, a, b, minutes):
    return f"{symbol} {NAMES[a]} {ASPECT_NAMES[symbol]} {NAMES[b]} орбис: {minutes // 60}° {minutes % 60}'"


def edge(a, b, symbol):
    return {'from_body': a, 'to_body': b, 'aspect': symbol}


def structured(lines):
    major = [l for l in lines if l.split(' ', 1)[0] in MAJOR]
    minor = [l for l in lines if l.split(' ', 1)[0] not in MAJOR]
    return {'major': major or ['Нет мажорных аспектов'], 'minor': minor or ['Нет минорных аспектов']}


ASPECT_COLUMNS = ['body_a', 'aspect', 'body_b', 'orb_deg', 'kind']


def sent(kwargs, key):
    """A provider argument that was really sent (the SDK stream helper fills the rest with NOT_GIVEN)."""
    value = kwargs.get(key)
    return None if value is NOT_GIVEN else value


def projection(rows):
    return {(frozenset((r[0], r[2])), r[1], None if r[3] is None else round(r[3], 1), r[4]) for r in rows}


class ChatQualityBase(unittest.TestCase):
    setUp = fixtures.MyChartsTests.setUp
    tearDown = fixtures.MyChartsTests.tearDown
    chart = fixtures.MyChartsTests.chart
    ask = conv.AIConversationTests.ask
    snapshot = conv.AIConversationTests.snapshot

    def long_chat(self, cid, pairs=12):
        """Like test_ai_conversation's helper, but may be called several times in one test."""
        with self.sessions() as db:
            if db.get(User, 1).plan != 'premium':
                now = datetime.utcnow()
                usage.set_plan(db, 1, 'premium', now - timedelta(days=1), now + timedelta(days=29))
            for i in range(pairs):
                db.add(GPTMessage(chart_id=cid, role='user', content=f'Question-{i:02d} '))
                db.add(GPTMessage(chart_id=cid, role='gpt', content=f'Answer-{i:02d}'))
            db.commit()

    def aspect_rows(self, ctx):
        self.assertEqual(ctx.get('aspect_columns'), ASPECT_COLUMNS)
        return ctx['aspects']

    def store(self, cid, bodies, circle=(), structured_lines=None, patterns=(), houses='default', **chart_fields):
        with self.sessions() as db:
            data = db.query(ChartData).filter_by(chart_id=cid).one()
            data.bodies_for_circle = bodies
            data.aspects_for_circle = list(circle)
            data.aspects_structured = structured_lines
            data.patterns_data = list(patterns)
            data.houses = cusps() if houses == 'default' else houses
            data.house_system = 'Placidus'
            for key, value in chart_fields.items():
                setattr(db.get(NatalChart, cid), key, value)
            db.commit()

    def context(self, cid):
        with self.sessions() as db:
            return json.loads(build_chart_context(db, 1, cid))

    def used(self):
        state = self.client.get('/account/usage', headers=self.owner).json()
        return state['gpt_messages_used'], state['gpt_messages_reserved']


class ChartContextV2Tests(ChatQualityBase):
    def test_cq1_degree_in_sign_is_floored_not_rounded_and_no_absolute_longitudes(self):
        """CQ-1: 353.0846 -> 23 deg 05 min Pisces; 29.5667 -> 29 deg 34 min (never 30); 0.0 -> 0 deg 00 min."""
        cid = self.chart()
        self.store(cid, {'☉': body('☉', 353.0846, 12), '☽': body('☽', 29.5667, 1), '♂': body('♂', 0.0, 1, True)})
        ctx = self.context(cid)
        self.assertEqual(ctx['placement_columns'], ['body', 'name', 'sign', 'degree_in_sign', 'house', 'retrograde'])
        rows = {row[0]: row for row in ctx['placements']}
        self.assertEqual(rows['☉'], ['☉', 'Солнце', 'Рыбы', '23°05′', 12, False])
        self.assertEqual(rows['☽'], ['☽', 'Луна', 'Овен', '29°34′', 1, False])
        self.assertEqual(rows['♂'], ['♂', 'Марс', 'Овен', '0°00′', 1, True])
        for row in ctx['placements']:
            self.assertFalse(any(isinstance(cell, float) for cell in row), row)
        text = json.dumps(ctx, ensure_ascii=False)
        self.assertNotIn('353.0846', text)
        self.assertNotIn('ecliptic_longitude_deg', text)
        self.assertNotIn('"30°"', text)

    def test_cq1_cusps_carry_sign_and_degree_not_raw_longitudes(self):
        cid = self.chart()
        self.store(cid, {'☉': body('☉', 10.0)})
        ctx = self.context(cid)
        self.assertNotIn('cusp_longitudes_deg', ctx['houses'])
        self.assertEqual(ctx['houses']['system'], 'Placidus')
        self.assertEqual(len(ctx['houses']['cusps']), 12)
        self.assertEqual(ctx['houses']['cusps'][0], ['1', 'Овен', '11°38′'])
        self.assertEqual(ctx['houses']['cusps'][1], ['2', 'Телец', '11°38′'])
        self.assertEqual(ctx['houses']['cusps'][11], ['12', 'Рыбы', '11°38′'])

    def test_cq2_names_orbs_dedup_and_no_raw_glyphs_in_aspect_rows(self):
        """CQ-2: one row per aspect (both directions collapse), names instead of glyphs, orb parsed from the saved line."""
        cid = self.chart()
        order = ['☉', '☽', '☿', '♀', '♂', '♃', '♄', '♅', '♆', '♇', 'W', 'X', '⚸', '⚷', 'AS', 'DS', 'MC', 'IC', '☊', '☋']
        bodies = spread(order)
        circle = [edge('☉', '♂', '□'), edge('♂', '☉', '□')]          # forward and backward: one row
        lines = ["□ Солнце квадрат Марс орбис: 3° 13'"]
        pairs = list(zip(order[:13], order[1:14]))                    # every glyph, every kind of body incl. W and X
        for i, (symbol, name) in enumerate(ASPECT_NAMES.items()):
            a, b = pairs[i] if i % 2 == 0 else pairs[i][::-1]
            circle.append(edge(a, b, symbol))
            lines.append(line(symbol, a, b, (i + 1) * 6))
        circle += [edge('AS', '☉', '△'), edge('☉', 'AS', '△')]
        lines.append(line('△', 'AS', '☉', 90))
        for a, b in (('AS', 'DS'), ('MC', 'IC'), ('☊', '☋')):         # defining axes carry no information
            circle.append(edge(a, b, '☍'))
            lines.append(line('☍', a, b, 0))
        self.store(cid, bodies, circle, structured(lines))
        ctx = self.context(cid)
        rows = self.aspect_rows(ctx)
        self.assertIn(['Солнце', 'квадрат', 'Марс', 3.2, 'major'], rows)
        expected = {(frozenset(('Солнце', 'Марс')), 'квадрат', 3.2, 'major'),
                    (frozenset(('Асцендент', 'Солнце')), 'трин', 1.5, 'major')}
        for i, (symbol, name) in enumerate(ASPECT_NAMES.items()):
            a, b = pairs[i] if i % 2 == 0 else pairs[i][::-1]
            expected.add((frozenset((NAMES[a], NAMES[b])), name, round((i + 1) * 0.1, 1),
                          'major' if symbol in MAJOR else 'minor'))
        self.assertEqual(projection(rows), expected)
        self.assertEqual(len(rows), len(expected), 'a pair must appear once, not once per direction')
        for row in rows:
            self.assertEqual(len(row), 5)
            for cell in row[:3]:
                self.assertNotIn(cell, RAW_GLYPHS, row)
            self.assertIn(row[1], ASPECT_NAMES.values())
        axes = {frozenset(('Асцендент', 'Десцендент')), frozenset(('Средина Неба', 'Надир')),
                frozenset(('Северный Узел', 'Южный Узел'))}
        self.assertFalse([r for r in rows if frozenset((r[0], r[2])) in axes])

    def test_cq2_patterns_use_body_names(self):
        cid = self.chart()
        pattern = {'type': 'Стеллиум в знаке Дева', 'bodies': [{'symbol': s} for s in ('☉', '♀', '♂', '☿')]}
        self.store(cid, spread(['☉', '☿', '♀', '♂']), patterns=[pattern, pattern])
        ctx = self.context(cid)
        self.assertEqual(ctx['pattern_columns'], ['type', 'bodies'])
        self.assertEqual(len(ctx['patterns']), 1)
        self.assertEqual(ctx['patterns'][0][0], 'Стеллиум в знаке Дева')
        self.assertEqual(set(ctx['patterns'][0][1]), {'Венера', 'Марс', 'Меркурий', 'Солнце'})

    def test_cq3_importance_order_groups_then_orb_with_unknown_orb_last(self):
        """CQ-3: major with core points, other major, minor with core points, other minor; orb ascending inside."""
        cid = self.chart()
        bodies = spread(['☉', '☽', '♂', '♃', '♄', '♅', '♆', '♇'])
        group1 = [('☽', '♄', '□', 18), ('☉', '♃', '△', 150), ('♂', '♅', '☌', None)]
        group2 = [('♅', '♆', '□', 10), ('♃', '♄', '☍', 60)]
        group3 = [('☽', '♃', 'Q', 6), ('☉', '♇', '∠', 40)]
        group4 = [('♅', '♇', 'S', 60), ('♄', '♆', '⚻', 180)]
        scrambled = [group4[1], group3[1], group2[1], group1[2], group4[0], group1[1], group3[0], group2[0], group1[0]]
        circle, lines = [], []
        for a, b, symbol, minutes in scrambled:
            circle.append(edge(a, b, symbol))
            if minutes is not None:   # reversed participants in the saved text: the pair must still be matched
                lines.append(line(symbol, b, a, minutes))
        self.store(cid, bodies, circle, structured(lines))
        rows = self.aspect_rows(self.context(cid))
        wanted = [group1[0], group1[1], group1[2], group2[0], group2[1], group3[0], group3[1], group4[0], group4[1]]
        got = [(frozenset((r[0], r[2])), r[3], r[4]) for r in rows]
        want = [(frozenset((NAMES[a], NAMES[b])), None if m is None else round(m / 60, 1),
                 'major' if s in MAJOR else 'minor') for a, b, s, m in wanted]
        self.assertEqual(len(rows), 9)
        self.assertEqual([(p, None if o is None else round(o, 1), k) for p, o, k in got], want)

    def test_cq4_unknown_orb_is_null_not_invented(self):
        """CQ-4: no saved orb text means orb_deg null and the row stays."""
        cid = self.chart()
        bodies = spread(['☉', '☽', '♂'])
        circle = [edge('☉', '♂', '□')]
        cases = [None, {'major': ['Нет мажорных аспектов'], 'minor': ['Нет минорных аспектов']},
                 structured([line('△', '☉', '☽', 60)])]   # text for another pair only
        for saved in cases:
            with self.subTest(saved=saved):
                self.store(cid, bodies, circle, saved)
                rows = self.aspect_rows(self.context(cid))
                self.assertEqual(rows, [['Солнце', 'квадрат', 'Марс', None, 'major']])

    def test_cq4_missing_cusps_stay_unknown(self):
        cid = self.chart()
        self.store(cid, {'☉': body('☉', 10.0)}, houses=None)
        ctx = self.context(cid)
        self.assertIn('cusps', ctx['houses'])
        self.assertIsNone(ctx['houses']['cusps'])

    def test_cq5_big_chart_fits_and_overflow_drops_aspects_from_the_tail(self):
        """CQ-5: 26 bodies / ~250 aspects fit in 24000 bytes; a tighter budget cuts whole rows from the end."""
        self.assertEqual(getattr(ai_chart_context, 'CHART_CONTEXT_BUDGET', None), 24000)
        cid = self.chart()
        order = list(NAMES)
        self.assertEqual(len(order), 26)
        bodies = spread(order)
        symbols = list(ASPECT_NAMES)
        circle, lines = [], []
        for i, (a, b) in enumerate(list(combinations(order, 2))[:250]):
            symbol = symbols[i % 13]
            circle.append(edge(a, b, symbol))
            lines.append(line(symbol, a, b, (i * 7) % 240))
        self.store(cid, bodies, circle, structured(lines))
        with self.sessions() as db:
            full_text = build_chart_context(db, 1, cid)
        full = json.loads(full_text)
        self.assertLessEqual(len(full_text.encode('utf-8')), 24000)
        self.assertEqual(full.get('omitted'), {'aspects': 0})
        self.assertGreater(len(self.aspect_rows(full)), 200)
        budget = len(full_text.encode('utf-8')) - 1500
        with patch.object(ai_chart_context, 'CHART_CONTEXT_BUDGET', budget, create=True), self.sessions() as db:
            short_text = build_chart_context(db, 1, cid)
            self.assertEqual(short_text, build_chart_context(db, 1, cid))
        short = json.loads(short_text)
        self.assertLessEqual(len(short_text.encode('utf-8')), budget)
        self.assertGreater(short['omitted']['aspects'], 0)
        self.assertEqual(len(short['aspects']) + short['omitted']['aspects'], len(full['aspects']))
        self.assertEqual(short['aspects'], full['aspects'][:len(short['aspects'])])
        self.assertEqual(short['placements'], full['placements'])
        self.assertEqual(len(short['placements']), 26)
        self.assertEqual(short['houses'], full['houses'])
        self.assertEqual(len(short['houses']['cusps']), 12)
        with patch.object(ai_chart_context, 'CHART_CONTEXT_BUDGET', 500, create=True), self.sessions() as db:
            with self.assertRaises(HTTPException) as error:
                build_chart_context(db, 1, cid)
        self.assertEqual(error.exception.status_code, 422)
        self.assertEqual(error.exception.detail, 'Данные карты превышают бюджет AI-контекста; требуется проверка карты.')

    def test_cq6_context_only_reads_saved_data_and_reports_missing_asteroids(self):
        """CQ-6: no Ephemeris/Aspects/swe call; no coordinates or city for the provider; missing asteroids named."""
        cid = self.chart()
        asteroids = ['⚳', '⚴', '⚵', '⚶']                              # Хирон and Фол were not calculated
        self.store(cid, spread(['☉', '☽'] + asteroids), [edge('☉', '☽', '△')],
                   structured([line('△', '☉', '☽', 60)]),
                   lat=47.318201, lon=8.541637, city='Канареечный-Город', region='Канареяния', country='Канарландия')
        def forbidden(*args, **kwargs):
            raise AssertionError('AI context must only read saved data')
        with patch.object(Ephemeris, '__init__', forbidden), patch.object(Aspects, '__init__', forbidden), \
                patch.object(swisseph, 'houses', forbidden), patch.object(swisseph, 'houses_ex', forbidden), \
                patch.object(swisseph, 'calc_ut', forbidden):
            with self.sessions() as db:
                text = build_chart_context(db, 1, cid)
        ctx = json.loads(text)
        for canary in ('47.318201', '8.541637', 'Канареечный', 'Канареяния', 'Канарландия'):
            self.assertNotIn(canary, text)
        for key in ('lat', 'lon', 'location'):
            self.assertNotIn(key, ctx['birth'])
        self.assertEqual(ctx['birth']['date'], [2000, 1, 2])
        self.assertEqual(ctx['birth']['timezone'], 'UTC')
        self.assertEqual(set(ctx['not_calculated']), {'Хирон', 'Фол'})

    def test_cq6_complete_chart_has_nothing_in_not_calculated(self):
        cid = self.chart()
        self.store(cid, spread(['☉', '⚳', '⚴', '⚵', '⚶', '⚷', 'φ']))
        self.assertFalse(self.context(cid).get('not_calculated'))


class RelationshipContextTests(unittest.TestCase):
    setUp = fixtures.MyChartsTests.setUp
    tearDown = fixtures.MyChartsTests.tearDown

    def pair(self, offsets):
        with self.sessions() as db:
            charts, saved = [], []
            for offset in offsets:
                info, data = facts(offset=offset)
                chart = NatalChart(user_id=1, birth_utc=info.birth_utc, **fixtures.PAYLOAD)
                db.add(chart); db.flush()
                stored = ChartData(chart_id=chart.id, bodies_for_circle=data.bodies_for_circle,
                                   aspects_for_circle=[{'from_body': '☉', 'to_body': '☽', 'aspect': '⚹'}],
                                   houses=data.houses, house_system=data.house_system)
                db.add(stored); charts.append(chart); saved.append(stored)
            relation = Relationship(user_id=1, chart_a_id=charts[0].id, chart_b_id=charts[1].id,
                person_a_label='Анна', person_b_label='Вячеслав', speaker_person='B',
                calculation=build_synastry_snapshot(charts[0], saved[0], charts[1], saved[1]),
                ruleset_version='synastry-major-8-v1')
            db.add(relation); db.flush()
            identifier = relation.id
            db.commit()
            return identifier

    def test_cq7_pair_placements_show_degree_in_sign_and_a_name_legend(self):
        """CQ-7: A has Sun at 23.0846 (23 deg 05 min), B at 29.5667 (29 deg 34 min, never 30)."""
        rid = self.pair((23.0846, 29.5667))
        with self.sessions() as db:
            text = build_relationship_context(db, 1, rid)
            self.assertEqual(text, build_relationship_context(db, 1, rid))
        ctx = json.loads(text)
        expected = {'A': {'☉': '23°05′', '☽': '24°05′', 'AS': '23°05′'}, 'B': {'☉': '29°34′', '☽': '0°34′'}}
        for person, wanted in expected.items():
            columns = ctx['people'][person]['placement_columns']
            self.assertNotIn('longitude_deg', columns)
            self.assertIn('degree_in_sign', columns)
            index = columns.index('degree_in_sign')
            rows = {row[0]: row for row in ctx['people'][person]['placements']}
            for symbol, degree in wanted.items():
                self.assertEqual(rows[symbol][index], degree, (person, symbol))
            for row in rows.values():
                self.assertRegex(row[index], r'^\d{1,2}°\d{2}′$')
        for symbol in ('☉', '☽', '☿', '♀', '♂', '♃', '♄', '♅', '♆', '♇'):   # the ten planets; angle naming is the author's call
            self.assertIn(NAMES[symbol], text, f'legend must name {symbol}')
        for symbol in ('☌', '⚹', '□', '△', '☍'):
            self.assertIn(ASPECT_NAMES[symbol], text, f'legend must name {symbol}')

    def test_cq7_budget_truncation_still_drops_whole_rows_deterministically(self):
        rid = self.pair((0, 30))
        with self.sessions() as db:
            full = build_relationship_context(db, 1, rid)
            budget = len(full.encode('utf-8')) - 500
            with patch('modules.ai_relationship_context.RELATIONSHIP_CONTEXT_BUDGET', budget):
                short = build_relationship_context(db, 1, rid)
                self.assertEqual(short, build_relationship_context(db, 1, rid))
            rows = json.loads(full)['interchart_aspects']
            kept = json.loads(short)['interchart_aspects']
            self.assertGreater(json.loads(short)['omitted']['interchart_aspects'], 0)
            self.assertEqual(kept, rows[:len(kept)])
            with patch('modules.ai_relationship_context.RELATIONSHIP_CONTEXT_BUDGET', 10):
                with self.assertRaises(HTTPException) as error:
                    build_relationship_context(db, 1, rid)
            self.assertEqual(error.exception.status_code, 422)


class ProviderParameterTests(ChatQualityBase):
    def env(self, **values):
        base = {k: v for k, v in os.environ.items() if k != 'OPENAI_REASONING_EFFORT'}
        return patch.dict(os.environ, {**base, **values}, clear=True)

    def answer(self, text='Ответ'):
        return reply(json.dumps({'answer': text, 'follow_up_suggestions': []}))

    def test_cq8_default_mode_ceilings_and_sampling(self):
        """CQ-8: max_completion_tokens 2000 / 900, no max_tokens, temperature 0.7 and no repetition penalties."""
        cid = self.chart()
        self.long_chat(cid)
        with self.env(), patch('modules.interpretation.client.chat.completions.create',
                               side_effect=[reply('Память'), self.answer()]) as api:
            self.assertEqual(self.ask(cid).status_code, 200)
        summary, answer = (call.kwargs for call in api.call_args_list)
        self.assertEqual(sent(answer, 'max_completion_tokens'), 2000)
        self.assertEqual(sent(summary, 'max_completion_tokens'), 900)
        for kwargs in (summary, answer):
            for absent in ('max_tokens', 'presence_penalty', 'frequency_penalty', 'reasoning_effort'):
                self.assertIsNone(sent(kwargs, absent), absent)
            self.assertEqual(kwargs['model'], OPENAI_PARAMS['model'])
        self.assertEqual(sent(answer, 'temperature'), 0.7)

    def stream_call(self, **env):
        cid = self.chart()
        with self.env(**env), patch('modules.interpretation.client.chat.completions.create',
                                    return_value=ProviderStream()) as api:
            response = self.client.post('/ask-gpt', headers={**self.owner, 'Accept': 'text/event-stream'},
                                        json={'chart_id': cid, 'question': 'Как мне двигаться дальше?'})
        self.assertEqual(response.status_code, 200)
        return api.call_args.kwargs

    def test_cq8_streaming_call_uses_the_same_ceiling_and_sampling(self):
        kwargs = self.stream_call()
        self.assertTrue(kwargs['stream'])
        self.assertEqual(sent(kwargs, 'max_completion_tokens'), 2000)
        self.assertEqual(sent(kwargs, 'temperature'), 0.7)
        for absent in ('max_tokens', 'presence_penalty', 'frequency_penalty', 'reasoning_effort'):
            self.assertIsNone(sent(kwargs, absent), absent)

    def test_cq8_streaming_call_in_reasoning_mode(self):
        kwargs = self.stream_call(OPENAI_REASONING_EFFORT='low')
        self.assertEqual(sent(kwargs, 'reasoning_effort'), 'low')
        self.assertEqual(sent(kwargs, 'max_completion_tokens'), 4000)
        for absent in ('max_tokens', 'temperature', 'presence_penalty', 'frequency_penalty'):
            self.assertIsNone(sent(kwargs, absent), absent)

    def test_cq8_reasoning_mode_follows_the_environment_without_reload(self):
        """CQ-8: OPENAI_REASONING_EFFORT set -> reasoning_effort, no temperature/penalties anywhere, +2000 unless none."""
        for effort, extra in (('low', 2000), ('none', 0)):
            with self.subTest(effort=effort):
                cid = self.chart()
                self.long_chat(cid)
                with self.env(OPENAI_REASONING_EFFORT=effort), patch(
                        'modules.interpretation.client.chat.completions.create',
                        side_effect=[reply('Память'), self.answer()]) as api:
                    self.assertEqual(self.ask(cid).status_code, 200)
                summary, answer = (call.kwargs for call in api.call_args_list)
                self.assertEqual(sent(answer, 'max_completion_tokens'), 2000 + extra)
                self.assertEqual(sent(summary, 'max_completion_tokens'), 900 + extra)
                for kwargs in (summary, answer):
                    self.assertEqual(sent(kwargs, 'reasoning_effort'), effort)
                    for absent in ('temperature', 'presence_penalty', 'frequency_penalty', 'max_tokens'):
                        self.assertIsNone(sent(kwargs, absent), absent)
        with self.env(), patch('modules.interpretation.client.chat.completions.create',
                               return_value=self.answer()) as api:
            self.assertEqual(self.ask(self.chart()).status_code, 200)
        self.assertIsNone(sent(api.call_args.kwargs, 'reasoning_effort'))   # unset again: back to the default mode
        self.assertEqual(sent(api.call_args.kwargs, 'temperature'), 0.7)


class TruncatedStream(ProviderStream):
    """Provider stream that stops in the middle of the JSON answer with the given finish_reason."""
    def __init__(self, finish='length', text='Начало ответа про карту КАНАРЕЙКА-ОТВЕТ-4419, который оборвался на полуслове'):
        super().__init__(finish=finish)
        self.text = text

    def __iter__(self):
        from openai.types.chat import ChatCompletionChunk
        raw = '{"answer":' + json.dumps(self.text, ensure_ascii=True)       # no closing quote or brace
        for start in range(0, len(raw), 7):
            self.read += 1
            yield ChatCompletionChunk(id='t', object='chat.completion.chunk', created=1, model='gpt-4o-mini',
                choices=[{'index': 0, 'delta': {'role': 'assistant', 'content': raw[start:start + 7]}, 'finish_reason': None}])
        yield ChatCompletionChunk(id='t', object='chat.completion.chunk', created=1, model='gpt-4o-mini',
            choices=[{'index': 0, 'delta': {}, 'finish_reason': self.finish}])
        yield ChatCompletionChunk(id='t', object='chat.completion.chunk', created=1, model='gpt-4o-mini',
            choices=[], usage={'prompt_tokens': 100, 'completion_tokens': 2000, 'total_tokens': 2100})


class TruncationTests(ChatQualityBase):
    def run_stream(self, cid, provider, question='Расскажи подробно про мою карту'):
        with self.sessions() as db:
            rid = usage.reserve(db, 1, cid)
        with patch('modules.interpretation.client.chat.completions.create', return_value=provider):
            events = list(chat_events(self.engine, 1, cid, question, rid, client, OPENAI_PARAMS))
        with self.sessions() as db:
            return events, db.get(GPTUsage, rid).status

    def test_cq9_stream_cut_by_length_says_so_and_releases_the_quota(self):
        cid = self.chart()
        events, status = self.run_stream(cid, TruncatedStream('length'))
        self.assertEqual(events[-1], {'type': 'error', 'status': 502, 'detail': TRUNCATED})
        self.assertNotIn('done', [e['type'] for e in events])
        self.assertEqual(status, 'released')
        with self.sessions() as db:
            self.assertEqual(db.query(GPTMessage).filter_by(chart_id=cid).count(), 2)   # only the fixture pair

    def test_cq9_stream_blocked_by_content_filter_has_its_own_text(self):
        cid = self.chart()
        events, status = self.run_stream(cid, TruncatedStream('content_filter'))
        self.assertEqual(events[-1], {'type': 'error', 'status': 502, 'detail': FILTERED})
        self.assertEqual(status, 'released')

    def test_cq9_network_cut_keeps_the_old_text(self):
        cid = self.chart()
        events, status = self.run_stream(cid, ProviderStream(fail=True))
        self.assertEqual(events[-1], {'type': 'error', 'status': 502, 'detail': NETWORK_CUT})
        self.assertEqual(status, 'released')

    def test_cq9_json_answer_cut_by_length_or_filter_is_502_with_the_new_text_and_no_charge(self):
        cid = self.chart()
        before = self.used()
        for finish, text in (('length', TRUNCATED), ('content_filter', FILTERED)):
            with self.subTest(finish=finish), patch('modules.interpretation.client.chat.completions.create',
                    return_value=reply('{"answer":"Начало ответа', finish=finish)) as api:
                response = self.ask(cid, 'Расскажи подробно про мою карту')
                api.assert_called_once()
            self.assertEqual(response.status_code, 502)
            self.assertEqual(response.json()['detail'], text)
            self.assertEqual(self.used(), before)
        self.assertEqual(self.snapshot(cid)[0], [('user', 'Existing question'), ('gpt', 'Existing answer')])


class SummaryNeverBlocksTests(ChatQualityBase):
    ANSWER = 'Ответ пришёл, несмотря на сводку.'

    def provider(self, summary):
        return patch('modules.interpretation.client.chat.completions.create',
                     side_effect=[summary, reply(json.dumps({'answer': self.ANSWER, 'follow_up_suggestions': []}))])

    def pending_note(self, messages):
        notes = [m['content'] for m in messages if m['content'].startswith('Older conversation memory')]
        self.assertEqual(len(notes), 1, 'the model must be told that older history is not summarised yet')
        return notes[0]

    def test_cq10_summary_cut_by_length_still_gives_the_answer(self):
        """CQ-10: summary finish_reason=length -> 200, answer saved, memory untouched, one unit, summary cost kept."""
        cid = self.chart()
        self.long_chat(cid)
        with self.sessions() as db:
            db.add(GPTConversation(chart_id=cid, user_id=1, summary='Original memory', through_message_id=0)); db.commit()
        before, (used, _) = self.snapshot(cid), self.used()
        with self.provider(reply('оборванная сводка', finish='length')) as api:
            response = self.ask(cid)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()['response'], self.ANSWER)
            self.assertEqual(api.call_count, 2, 'no repeated summary call in one request')
            note = self.pending_note(api.call_args_list[1].kwargs['messages'])
        self.assertIn('"older_history_pending":true', note)
        self.assertIn('Original memory', note)
        after = self.snapshot(cid)
        self.assertEqual(after[1], before[1])                               # cursor and summary as before
        self.assertEqual(after[0][:-2], before[0])
        self.assertEqual(after[0][-1], ('gpt', self.ANSWER))
        self.assertEqual(self.used(), (used + 1, 0))
        with self.sessions() as db:
            row = db.query(GPTUsage).filter(GPTUsage.model.is_not(None)).one()
            self.assertEqual(len(row.summary_usage), 1)
            self.assertEqual(row.summary_usage[0]['total_tokens'], 168)
        # The very next question is not blocked by the same stuck summary either.
        with self.provider(reply('опять оборванная сводка', finish='length')) as api:
            second = self.ask(cid, 'А что с работой?')
            self.assertEqual(second.status_code, 200, second.text)
            self.assertEqual(api.call_count, 2)
        self.assertEqual(self.used(), (used + 2, 0))
        self.assertEqual(self.snapshot(cid)[1], before[1])

    def test_cq10_summary_stopped_by_the_provider_filter_is_discarded_like_a_cut(self):
        """Review F1: finish_reason=content_filter on the SUMMARY call must not block the chat (same as length)."""
        cid = self.chart()
        self.long_chat(cid)
        with self.sessions() as db:
            db.add(GPTConversation(chart_id=cid, user_id=1, summary='Original memory', through_message_id=0)); db.commit()
        before, (used, _) = self.snapshot(cid), self.used()
        with self.provider(reply('сводка, остановленная фильтром', finish='content_filter')) as api:
            response = self.ask(cid)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()['response'], self.ANSWER)
            self.assertEqual(api.call_count, 2, 'no repeated summary call in one request')
            note = self.pending_note(api.call_args_list[1].kwargs['messages'])
        self.assertIn('"older_history_pending":true', note)
        self.assertIn('Original memory', note)
        after = self.snapshot(cid)
        self.assertEqual(after[1], before[1])                               # cursor and summary as before
        self.assertEqual(after[0][-1], ('gpt', self.ANSWER))
        self.assertEqual(self.used(), (used + 1, 0))
        with self.sessions() as db:
            row = db.query(GPTUsage).filter(GPTUsage.model.is_not(None)).one()
            self.assertEqual(len(row.summary_usage), 1)
            self.assertEqual(row.summary_usage[0]['total_tokens'], 168)
            self.assertIs(row.summary_usage[0].get('discarded'), True)
        # The next question is not blocked by the same stuck summary either.
        with self.provider(reply('опять фильтр', finish='content_filter')) as api:
            second = self.ask(cid, 'А что с работой?')
            self.assertEqual(second.status_code, 200, second.text)
            self.assertEqual(api.call_count, 2)
        self.assertEqual(self.used(), (used + 2, 0))
        self.assertEqual(self.snapshot(cid)[1], before[1])

    def test_cq10_summary_stopped_by_the_provider_filter_streams_a_done_event(self):
        cid = self.chart()
        self.long_chat(cid)
        with self.sessions() as db:
            db.add(GPTConversation(chart_id=cid, user_id=1, summary='Original memory', through_message_id=0)); db.commit()
            rid = usage.reserve(db, 1, cid)
        before = self.snapshot(cid)
        def provider(**kwargs):
            return ProviderStream() if kwargs.get('stream') else reply('сводка, остановленная фильтром', finish='content_filter')
        with patch('modules.interpretation.client.chat.completions.create', side_effect=provider) as api,                 self.assertLogs(level='WARNING') as logs:
            events = list(chat_events(self.engine, 1, cid, 'Как мне двигаться дальше?', rid, client, OPENAI_PARAMS))
        self.assertEqual(events[-1]['type'], 'done', events[-1])
        self.assertEqual(api.call_count, 2)
        self.assertEqual(self.snapshot(cid)[1], before[1])
        with self.sessions() as db:
            row = db.get(GPTUsage, rid)
            self.assertEqual(row.status, 'succeeded')
            self.assertIs(row.summary_usage[0].get('discarded'), True)
        self.assertEqual(len([r for r in logs.records if 'outcome=summary_degraded' in r.getMessage()]), 1, logs.output)
        self.assertFalse([r for r in logs.records if 'outcome=filtered' in r.getMessage()])

    def test_cq10_summary_without_prior_memory_is_flagged_pending_anyway(self):
        cid = self.chart()
        self.long_chat(cid)
        with self.provider(reply('оборванная сводка', finish='length')) as api:
            self.assertEqual(self.ask(cid).status_code, 200)
            note = self.pending_note(api.call_args_list[1].kwargs['messages'])
        self.assertIn('"older_history_pending":true', note)
        self.assertIsNone(self.snapshot(cid)[1])

    def test_cq10_summary_over_the_byte_budget_also_does_not_block(self):
        cid = self.chart()
        self.long_chat(cid)
        with self.sessions() as db:
            db.add(GPTConversation(chart_id=cid, user_id=1, summary='Original memory', through_message_id=0)); db.commit()
        before = self.snapshot(cid)
        with self.provider(reply('я' * 4000)) as api:          # 8000 bytes, SUMMARY_BUDGET is 6000
            response = self.ask(cid)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(api.call_count, 2)
            self.assertIn('"older_history_pending":true', self.pending_note(api.call_args_list[1].kwargs['messages']))
        self.assertEqual(self.snapshot(cid)[1], before[1])
        self.assertEqual(self.used(), (1, 0))
        with self.sessions() as db:
            self.assertEqual(len(db.query(GPTUsage).filter(GPTUsage.model.is_not(None)).one().summary_usage), 1)

    def test_cq10_summary_timeout_still_refuses_without_charge(self):
        cid = self.chart()
        self.long_chat(cid)
        with self.sessions() as db:
            db.add(GPTConversation(chart_id=cid, user_id=1, summary='Original memory', through_message_id=0)); db.commit()
        before, state = self.snapshot(cid), self.used()
        with patch('modules.interpretation.client.chat.completions.create', side_effect=[TimeoutError('mock')]):
            self.assertGreaterEqual(self.ask(cid).status_code, 500)
        self.assertEqual(self.snapshot(cid), before)
        self.assertEqual(self.used(), state)


class MemoryAndJournalTests(ChatQualityBase):
    CANARY_QUESTION = 'КАНАРЕЙКА-ВОПРОС-7731'

    def exchanges(self, cid):
        rows = []
        for i in range(3):
            filler = f'ОТВЕТ-{i} ' + ('Это длинный подробный ответ астролога про вашу карту. ' * 60)
            rows += [('user', f'Вопрос {i}: расскажи подробнее про тему {i}'), ('gpt', filler[:2500])]
        with self.sessions() as db:
            for role, text in rows:
                db.add(GPTMessage(chart_id=cid, role=role, content=text))
            db.commit()
        return rows

    def test_cq11_three_long_exchanges_are_remembered_verbatim_without_a_summary_call(self):
        cid = self.chart(related=True)
        with self.sessions() as db:
            db.query(GPTMessage).filter_by(chart_id=cid).delete(); db.commit()
        rows = self.exchanges(cid)
        self.assertGreater(sum(len(text.encode('utf-8')) for _, text in rows), 12000)   # too much for the old budget
        with patch('modules.interpretation.client.chat.completions.create',
                   return_value=reply(json.dumps({'answer': 'Хорошо.', 'follow_up_suggestions': []}))) as api:
            self.assertEqual(self.ask(cid, 'А что из этого главное?').status_code, 200)
            self.assertEqual(api.call_count, 1, 'a summary call means the dialogue did not fit verbatim')
            messages = api.call_args.kwargs['messages']
        history = messages[-7:-1]
        self.assertEqual([m['content'] for m in history], [text for _, text in rows])
        self.assertEqual([m['role'] for m in history], ['user', 'assistant'] * 3)
        self.assertEqual(messages[-1], {'role': 'user', 'content': 'А что из этого главное?'})
        self.assertFalse([m for m in messages if m['content'].startswith('Older conversation memory')])

    def outcome_lines(self, logs):
        return [r.getMessage() for r in logs.records if 'AI chat outcome=' in r.getMessage()]

    def test_cq11_truncated_answer_writes_exactly_one_outcome_line_without_any_text(self):
        cid = self.chart()
        with self.sessions() as db:
            rid = usage.reserve(db, 1, cid)
        with self.assertLogs(level='WARNING') as logs, patch(
                'modules.interpretation.client.chat.completions.create', return_value=TruncatedStream('length')):
            events = list(chat_events(self.engine, 1, cid, self.CANARY_QUESTION, rid, client, OPENAI_PARAMS))
        self.assertEqual(events[-1]['detail'], TRUNCATED)
        lines = self.outcome_lines(logs)
        self.assertEqual(len(lines), 1, logs.output)
        for part in ('AI chat outcome=truncated', 'subject=natal', 'model=', 'output_tokens=', 'duration_ms='):
            self.assertIn(part, lines[0])
        rendered = '\n'.join(logging.Formatter().format(record) + repr(record.args) for record in logs.records)
        for secret in (self.CANARY_QUESTION, 'КАНАРЕЙКА-ОТВЕТ-4419', 'Начало ответа'):
            self.assertNotIn(secret, rendered)
        for private in ('chart_id', 'user_id', f'chart={cid}', 'a@example.test'):
            self.assertNotIn(private, lines[0])

    def test_cq11_stream_ending_with_an_unexpected_finish_reason_logs_interrupted(self):
        """Review F2: finish_reason that is not stop/length/content_filter (here tool_calls) is an 'interrupted' outcome."""
        cid = self.chart()
        with self.sessions() as db:
            rid = usage.reserve(db, 1, cid)
        with self.assertLogs(level='WARNING') as logs, patch(
                'modules.interpretation.client.chat.completions.create', return_value=ProviderStream(finish='tool_calls')):
            events = list(chat_events(self.engine, 1, cid, self.CANARY_QUESTION, rid, client, OPENAI_PARAMS))
        self.assertEqual(events[-1]['type'], 'error')
        self.assertEqual(events[-1]['status'], 502)
        self.assertTrue(events[-1]['detail'].startswith('Ответ прервался'), events[-1])
        self.assertNotIn('done', [e['type'] for e in events])
        with self.sessions() as db:
            self.assertEqual(db.get(GPTUsage, rid).status, 'released')
            self.assertEqual(db.query(GPTMessage).filter_by(chart_id=cid).count(), 2)
        lines = self.outcome_lines(logs)
        self.assertEqual(len(lines), 1, logs.output)
        for part in ('AI chat outcome=interrupted', 'subject=natal', 'model=', 'output_tokens=', 'duration_ms='):
            self.assertIn(part, lines[0])
        rendered = '\n'.join(logging.Formatter().format(record) + repr(record.args) for record in logs.records)
        self.assertNotIn(self.CANARY_QUESTION, rendered)

    def test_cq11_filtered_answer_has_its_own_outcome_and_a_good_answer_none(self):
        cid = self.chart()
        with self.sessions() as db:
            rid = usage.reserve(db, 1, cid)
        with self.assertLogs(level='WARNING') as logs, patch(
                'modules.interpretation.client.chat.completions.create', return_value=TruncatedStream('content_filter')):
            list(chat_events(self.engine, 1, cid, 'Вопрос', rid, client, OPENAI_PARAMS))
        lines = self.outcome_lines(logs)
        self.assertEqual(len(lines), 1, logs.output)
        self.assertIn('outcome=filtered', lines[0])
        with self.sessions() as db:
            rid = usage.reserve(db, 1, cid)
        seen = []
        class Collect(logging.Handler):
            def emit(self, record):
                seen.append(record.getMessage())
        handler, root = Collect(level=logging.DEBUG), logging.getLogger()
        root.addHandler(handler)
        try:
            with patch('modules.interpretation.client.chat.completions.create', return_value=ProviderStream()):
                events = list(chat_events(self.engine, 1, cid, 'Вопрос', rid, client, OPENAI_PARAMS))
        finally:
            root.removeHandler(handler)
        self.assertEqual([m for m in seen if 'AI chat outcome=' in m], [], 'a normal answer is not logged')
        self.assertEqual(events[-1]['type'], 'done')


if __name__ == '__main__':
    unittest.main()
