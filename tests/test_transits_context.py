"""TRANSITS-1 chat context regression TC1-TC9, TF1 (DESIGN §2, §3, §8).

Clock: modules.transits.server_now is the only clock and is frozen at NOW. Durations (TC9) use a real
sleep with a wide margin, so the outcome does not depend on how the module reads its monotonic clock.
No network, no .secrets, no environment dumps; OpenAI is a stub.
"""
import hashlib
import json
import logging
import re
import time
import unittest
from contextlib import ExitStack, contextmanager
from datetime import datetime, timedelta
from itertools import combinations
from unittest.mock import patch
from zoneinfo import ZoneInfo

import swisseph

import test_chat_quality as cq
import test_relationship_ai as relationships
import test_transits_jpl as J
from test_ai_stream import ProviderStream
from test_transits_accuracy import NOW, POINTS, require_transits
from database.queries import GPTUsage, NatalChart
from modules import ai_chart_context, usage
from modules.ai_chart_context import build_chart_context, degree_in_sign, sign_of
from modules.aspects import Aspects
from modules.cusp_math import house_for_longitude
from modules.ephemeris import Ephemeris

utc = J.utc
KYIV = ZoneInfo('Europe/Kyiv')
BIRTH_UTC = datetime(2000, 1, 2, 10, 0)          # naive UTC, as stored; 12:00 in Kyiv
NATAL = {'☉': 146.5, '☽': 63.2, '☿': 130.0, '♀': 200.4, '♂': 15.7, '♃': 101.3, '♄': 250.9,
         '♅': 275.2, '♆': 283.6, '♇': 226.1, 'AS': 304.77, 'MC': 215.0}
SLOW_NAMES = ['Юпитер', 'Сатурн', 'Уран', 'Нептун', 'Плутон']
ASPECT_WORDS = {'соединение', 'секстиль', 'квадрат', 'трин', 'оппозиция'}
POINT_NAMES = {cq.NAMES[p] for p in POINTS}
GROUPS = [{'Солнце', 'Луна', 'Асцендент', 'Средина Неба'}, {'Меркурий', 'Венера', 'Марс'},
          {'Юпитер', 'Сатурн'}, {'Уран', 'Нептун', 'Плутон'}]
VERIFIED_LIMITATIONS = 'Saved natal calculations are never recalculated; dated sky data only in transits.'
LEGACY_LIMITATIONS = ('Legacy chart: birth timezone/minutes unverified; missing cusps remain unknown. '
                      'Do not infer or recalculate.')
UNAVAILABLE_LEGACY = {'status': 'unavailable', 'reason': 'legacy_chart'}
UNAVAILABLE_FAILED = {'status': 'unavailable', 'reason': 'calculation_failed'}
CANARY = 'КАНАРЕЙКА-7781 1990-05-03'
FAILED_LINE = re.compile(r'^AI transits outcome=failed reason=(ephemeris_error|missing_saved_positions|unclassified) '
                         r'duration_ms=\d+$')
SLOW_LINE = re.compile(r'^AI transits outcome=slow duration_ms=\d+ rows=\d+$')
ISO_DATE = re.compile(r'^\d{4}-\d{2}-\d{2}$')
SLOW_IDS = {swisseph.JUPITER, swisseph.SATURN, swisseph.URANUS, swisseph.NEPTUNE, swisseph.PLUTO}
# TC3 baseline: the natal part (context without "transits" and "limitations") of natal_bodies() on 483235c.
BASE_NATAL_PART_SHA256 = '178ddb6d7601e6bc87a2fc1c26b8c8297f6c0a3dff282f86848ae10905140db0'


def compact(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def natal_bodies(skip=()):
    return {s: cq.body(s, lon, house=i % 12 + 1, retrograde=s in ('♄', '♆'))
            for i, (s, lon) in enumerate(NATAL.items()) if s not in skip}


def model_context(provider_call):
    """The chart context the model received (messages[1], after the fixed caption line)."""
    content = provider_call.kwargs['messages'][1]['content']
    return json.loads(content.split('\n', 1)[1])


class _Lines(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


@contextmanager
def captured_logs():
    """Root handler at INFO for the block; restores the logger state afterwards."""
    root, handler = logging.getLogger(), _Lines()
    level = root.level
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    try:
        yield handler.lines
    finally:
        root.removeHandler(handler)
        root.setLevel(level)


def sse_types(text):
    return [json.loads(chunk[len('data: '):])['type'] for chunk in text.split('\n\n') if chunk.startswith('data: ')]


def transit_lines(lines):
    return [line for line in lines if line.startswith('AI transits')]


class TransitContextBase(cq.ChatQualityBase):
    def setUp(self):
        super().setUp()
        self.transits = require_transits(self)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(self.transits, 'server_now', return_value=NOW))

    def verified(self, bodies=None, houses='default', timezone='Europe/Kyiv', **extra):
        cid = self.chart()
        self.store(cid, natal_bodies() if bodies is None else bodies, houses=houses,
                   birth_utc=BIRTH_UTC, timezone=timezone, **extra)
        return cid

    def legacy(self, bodies=None, **fields):
        cid = self.chart()
        self.store(cid, natal_bodies() if bodies is None else bodies, **fields)
        return cid

    def premium(self):
        with self.sessions() as db:
            now = datetime.utcnow()
            usage.set_plan(db, 1, 'premium', now - timedelta(days=1), now + timedelta(days=29))

    def patch_find(self, **kwargs):
        """Replace find_transits wherever the context may look it up (module attribute or imported name)."""
        stack = ExitStack()
        mock = stack.enter_context(patch.object(self.transits, 'find_transits', **kwargs))
        if hasattr(ai_chart_context, 'find_transits'):
            stack.enter_context(patch.object(ai_chart_context, 'find_transits', mock))
        return stack

    def block(self, cid):
        ctx = self.context(cid)
        return ctx, ctx['transits']

    def synthetic(self, body, aspect, point, periods, exact, orb_now=None, target=0.0):
        return self.transits.Transit(body=body, aspect=aspect, point=point, target=target, orb_now=orb_now,
                                     periods=periods, exact=exact)


class ContextShapeTests(TransitContextBase):
    def test_tc1_verified_chart_gets_a_last_transits_block(self):
        cid = self.verified()
        ctx, block = self.block(cid)
        self.assertEqual(list(ctx)[-1], 'transits')
        self.assertEqual(block['status'], 'ok')
        self.assertEqual((block['today'], block['range_to'], block['dates_timezone'], block['orb_deg']),
                         ('2026-11-14', '2027-11-14', 'Europe/Kyiv', 1))
        self.assertEqual(block['sky_now_columns'], ['body', 'sign', 'degree_in_sign', 'retrograde', 'natal_house'])
        self.assertEqual(block['columns'], ['body', 'aspect', 'point', 'orb_now', 'periods', 'exact'])
        self.assertEqual(ctx['limitations'], VERIFIED_LIMITATIONS)
        self.assertNotIn('points_missing', block)
        self.assertEqual(block['omitted'], 0)
        sky = self.transits.sky_now(NOW)
        cusps = [c['degree'] for c in cq.cusps()]
        self.assertEqual([row[0] for row in block['sky_now']], SLOW_NAMES)
        for row, item in zip(block['sky_now'], sky):
            with self.subTest(body=row[0]):
                self.assertEqual(row, [row[0], sign_of(item.longitude), degree_in_sign(item.longitude),
                                       item.retrograde, house_for_longitude(item.longitude, cusps)])
                self.assertEqual(sign_of(J.lon_at(item.body, NOW)), row[1])
        self.assertEqual([row[3] for row in block['sky_now']], [False, True, True, True, False])
        self.assertTrue(block['rows'])
        for row in block['rows']:
            with self.subTest(row=row):
                body, aspect, point, orb, periods, exact = row
                self.assertIn(body, SLOW_NAMES)
                self.assertIn(aspect, ASPECT_WORDS)
                self.assertIn(point, POINT_NAMES)
                self.assertTrue(orb is None or (isinstance(orb, float) and orb == round(orb, 1) and orb <= 1.0))
                self.assertTrue(periods)
                for start, end in periods:
                    for value in (start, end):
                        self.assertTrue(value is None or ISO_DATE.match(value))
                self.assertLessEqual(len(exact), 3)
                self.assertTrue(all(ISO_DATE.match(x) for x in exact))
        # The Jupiter loop over the natal Sun (TA8 in Kyiv): JPL dates, nothing near midnight.
        self.assertIn(['Юпитер', 'соединение', 'Солнце', 0.8, [['2026-11-11', '2027-01-13'], ['2027-07-03', '2027-07-13']],
                       ['2026-11-25', '2026-12-31', '2027-07-08']], block['rows'])
        # No raw longitudes anywhere in the block: numbers carry at most one decimal.
        self.assertIsNone(re.search(r'\d\.\d{2,}', compact(block)))

    def test_tc2_legacy_chart_is_unavailable_without_any_ephemeris_call(self):
        def forbidden(*args, **kwargs):
            raise AssertionError('legacy charts must not be calculated')
        for fields in ({}, {'birth_utc': BIRTH_UTC, 'timezone': 'Mars/Olympus_Mons'},
                       {'birth_utc': BIRTH_UTC, 'timezone': ''}):
            with self.subTest(fields=fields):
                cid = self.legacy(**fields)
                with patch.object(swisseph, 'calc_ut', forbidden):
                    ctx, block = self.block(cid)
                self.assertEqual(block, UNAVAILABLE_LEGACY)
                self.assertEqual(list(ctx)[-1], 'transits')
                self.assertEqual(ctx['limitations'], LEGACY_LIMITATIONS)

    def test_tc2_verified_zone_rule(self):
        def chart(birth_utc, timezone):
            return NatalChart(birth_utc=birth_utc, timezone=timezone)
        self.assertEqual(self.transits.verified_zone(chart(BIRTH_UTC, 'Europe/Kyiv')), KYIV)
        for birth_utc, timezone in ((None, 'Europe/Kyiv'), (BIRTH_UTC, None), (BIRTH_UTC, ''),
                                    (BIRTH_UTC, 'Mars/Olympus_Mons'), ('2000-01-02T10:00:00', 'Europe/Kyiv')):
            with self.subTest(birth_utc=birth_utc, timezone=timezone):
                self.assertIsNone(self.transits.verified_zone(chart(birth_utc, timezone)))

    def test_tc3_natal_is_never_recalculated_and_only_slow_bodies_are_asked(self):
        cid = self.verified()
        calls = []
        real = swisseph.calc_ut

        def recording(jd, body, flags=swisseph.FLG_SWIEPH | swisseph.FLG_SPEED, *args):
            calls.append((body, flags))
            return real(jd, body, flags, *args)

        def forbidden(*args, **kwargs):
            raise AssertionError('the chat context must not recalculate the natal chart')
        with patch.object(Ephemeris, '__init__', forbidden), patch.object(Aspects, '__init__', forbidden), \
                patch.object(swisseph, 'houses', forbidden), patch.object(swisseph, 'houses_ex', forbidden), \
                patch('modules.ephemeris.Horizons', forbidden), \
                patch('astroquery.jplhorizons.Horizons', forbidden), \
                patch.object(swisseph, 'calc_ut', recording):
            ctx, block = self.block(cid)
        self.assertEqual(block['status'], 'ok')
        self.assertTrue(calls)
        self.assertLessEqual({body for body, _ in calls}, SLOW_IDS)
        self.assertEqual({flags for _, flags in calls}, {swisseph.FLG_SWIEPH | swisseph.FLG_SPEED})
        natal_part = {k: v for k, v in ctx.items() if k not in ('transits', 'limitations')}
        self.assertEqual(hashlib.sha256(compact(natal_part).encode('utf-8')).hexdigest(), BASE_NATAL_PART_SHA256)

    def test_tc6_same_clock_same_text(self):
        cid = self.verified()
        with self.sessions() as db:
            first = build_chart_context(db, 1, cid)
            second = build_chart_context(db, 1, cid)
        self.assertEqual(first, second)
        self.assertEqual(json.loads(first)['transits']['status'], 'ok')

    def test_tc7_missing_angles_are_named_and_bad_cusps_give_no_house(self):
        cid = self.verified(bodies=natal_bodies(skip=('AS', 'MC')))
        _, block = self.block(cid)
        self.assertEqual(block['status'], 'ok')
        self.assertEqual(set(block['points_missing']), {'Асцендент', 'Средина Неба'})
        self.assertFalse({row[2] for row in block['rows']} & {'Асцендент', 'Средина Неба'})
        broken = natal_bodies()
        broken['☽']['degree'] = None
        cid = self.verified(bodies=broken)
        _, block = self.block(cid)
        self.assertEqual(block['points_missing'], ['Луна'])
        self.assertNotIn('Луна', {row[2] for row in block['rows']})
        for houses in ([], cq.cusps()[:11], None):
            with self.subTest(houses=houses):
                cid = self.verified(houses=houses)
                _, block = self.block(cid)
                self.assertEqual([row[4] for row in block['sky_now']], [None] * 5)

    def test_tc8_open_ends_are_null_and_rows_follow_the_design_order(self):
        t = self.synthetic
        rows = [
            t('♅', '△', '♅', [(utc(2027, 3, 1, 12), utc(2027, 5, 1, 12))], [utc(2027, 4, 1, 12)]),
            t('♄', '□', '☿', [(None, utc(2027, 1, 10, 12))], [], orb_now=0.1),   # tightest orb, later point group
            t('♃', '☌', '☽', [(utc(2027, 6, 1, 12), None)], []),
            t('♆', '⚹', 'AS', [(utc(2026, 10, 1, 12), utc(2027, 2, 1, 12))], [utc(2026, 11, 20, 12)], orb_now=0.5),
            t('♇', '☍', '♀', [(utc(2027, 2, 1, 12), utc(2027, 3, 1, 12))], [utc(2027, 2, 15, 12)]),
            t('♃', '☌', '☉', [(utc(2026, 11, 1, 12), utc(2026, 12, 1, 12))], [utc(2026, 11, 13, 23, 30)], orb_now=0.2),
        ]
        cid = self.verified()
        with self.patch_find(return_value=rows):
            _, block = self.block(cid)
        self.assertEqual(block['rows'], [
            ['Юпитер', 'соединение', 'Солнце', 0.2, [['2026-11-01', '2026-12-01']], ['2026-11-14']],
            ['Нептун', 'секстиль', 'Асцендент', 0.5, [['2026-10-01', '2027-02-01']], ['2026-11-20']],
            ['Сатурн', 'квадрат', 'Меркурий', 0.1, [[None, '2027-01-10']], []],
            ['Юпитер', 'соединение', 'Луна', None, [['2027-06-01', None]], []],
            ['Плутон', 'оппозиция', 'Венера', None, [['2027-02-01', '2027-03-01']], ['2027-02-15']],
            ['Уран', 'трин', 'Уран', None, [['2027-03-01', '2027-05-01']], ['2027-04-01']],
        ])
        self.assertEqual(block['omitted'], 0)


class BudgetTests(TransitContextBase):
    def many(self, count):
        bodies, aspects = ['♃', '♄', '♅', '♆', '♇'], ['☌', '⚹', '□', '△', '☍']
        rows = []
        for i in range(count):
            begin = utc(2027, 1, 1, 12) + timedelta(days=3 * i)
            rows.append(self.synthetic(bodies[i % 5], aspects[(i // 5) % 5], POINTS[i % 12],
                                       [(begin, begin + timedelta(days=40)), (begin + timedelta(days=150), None)],
                                       [begin + timedelta(days=10), begin + timedelta(days=20), begin + timedelta(days=170)]))
        return rows

    def test_tc4_block_budget_drops_rows_from_the_tail(self):
        self.assertEqual(getattr(ai_chart_context, 'TRANSIT_BLOCK_BUDGET', None), 4000)
        self.assertEqual(ai_chart_context.CHART_CONTEXT_BUDGET, 24000)
        cid = self.verified()
        rows = self.many(80)
        with self.patch_find(return_value=rows):
            _, block = self.block(cid)
            with patch.object(ai_chart_context, 'TRANSIT_BLOCK_BUDGET', 10 ** 6):
                _, full = self.block(cid)
        self.assertLessEqual(len(compact(block).encode('utf-8')), 4000)
        self.assertGreater(block['omitted'], 0)
        self.assertEqual(full['omitted'], 0)
        self.assertEqual(len(full['rows']), 80)
        self.assertEqual(len(block['rows']) + block['omitted'], 80)
        self.assertEqual(block['rows'], full['rows'][:len(block['rows'])])
        group = {name: i for i, names in enumerate(GROUPS) for name in names}
        future = full['rows']                                    # all future: ordered by group, then start date
        keys = [(group[r[2]], r[4][0][0]) for r in future]
        self.assertEqual(keys, sorted(keys))
        self.assertEqual(block['sky_now'], full['sky_now'])

    def test_tc4_big_verified_chart_fits_24000_and_keeps_placements_and_cusps(self):
        order = list(cq.NAMES)
        bodies = cq.spread(order)
        symbols = list(cq.ASPECT_NAMES)
        circle, lines = [], []
        for i, (a, b) in enumerate(list(combinations(order, 2))[:250]):
            circle.append(cq.edge(a, b, symbols[i % 13]))
            lines.append(cq.line(symbols[i % 13], a, b, (i * 7) % 240))
        legacy = self.chart()
        self.store(legacy, bodies, circle, cq.structured(lines))
        plain = self.context(legacy)
        everything = len(plain['aspects']) + plain['omitted']['aspects']
        cid = self.chart()
        self.store(cid, bodies, circle, cq.structured(lines), birth_utc=BIRTH_UTC, timezone='Europe/Kyiv')
        with self.sessions() as db:
            text = build_chart_context(db, 1, cid)
        ctx = json.loads(text)
        self.assertLessEqual(len(text.encode('utf-8')), 24000)
        self.assertEqual(ctx['transits']['status'], 'ok')
        self.assertLessEqual(len(compact(ctx['transits']).encode('utf-8')), 4000)
        self.assertEqual(len(ctx['placements']), 26)
        self.assertEqual(len(ctx['houses']['cusps']), 12)
        self.assertEqual(len(ctx['aspects']) + ctx['omitted']['aspects'], everything)


class FailureTests(TransitContextBase):
    def ask_both(self, cid):
        """One JSON and one SSE question; returns [(status, provider calls, context seen by the model)]."""
        results = []
        with patch('modules.interpretation.client.chat.completions.create',
                   return_value=relationships.reply(answer='Ответ по наталу')) as api:
            response = self.client.post('/ask-gpt', json={'chart_id': cid, 'question': 'Что меня ждёт в работе?'},
                                        headers=self.owner)
            results.append((response.status_code, api.call_count, api.call_args_list))
        with patch('modules.interpretation.client.chat.completions.create', return_value=ProviderStream()) as api:
            response = self.client.post('/ask-gpt', json={'chart_id': cid, 'question': 'Что меня ждёт в работе?'},
                                        headers={**self.owner, 'Accept': 'text/event-stream'})
            self.assertEqual(sse_types(response.text)[-1], 'done', response.text[-300:])
            results.append((response.status_code, api.call_count, api.call_args_list))
        return results

    def test_tc5_calculation_failure_answers_from_natal_and_logs_one_closed_line(self):
        self.premium()
        cid = self.verified()
        for error, reason in ((swisseph.Error('ephemeris file missing'), 'ephemeris_error'),
                              (RuntimeError(CANARY), 'unclassified')):
            with self.subTest(error=type(error).__name__):
                before = self.used()[0]
                with captured_logs() as lines, self.patch_find(side_effect=error):
                    results = self.ask_both(cid)
                for status, count, calls in results:
                    self.assertEqual(status, 200)
                    self.assertEqual(count, 1)
                    self.assertEqual(model_context(calls[0])['transits'], UNAVAILABLE_FAILED)
                    sent = compact(calls[0].kwargs['messages'])
                    for leak in ('КАНАРЕЙКА', '7781', '1990-05-03', 'ephemeris file missing'):
                        self.assertNotIn(leak, sent)
                self.assertEqual(self.used()[0], before + 2)
                found = transit_lines(lines)
                self.assertEqual(len(found), 2, found)                    # one line per request
                for line in found:
                    self.assertRegex(line, FAILED_LINE)
                    self.assertIn(f'reason={reason} ', line)
                for line in lines:                                       # nothing else leaks either
                    for leak in ('КАНАРЕЙКА', '7781', '1990-05-03', 'ephemeris file missing'):
                        self.assertNotIn(leak, line)
        with self.sessions() as db:
            self.assertEqual(db.query(GPTUsage).filter_by(status='reserved').count(), 0)   # nothing left hanging

    def test_tc5_direct_context_failure_is_unavailable_not_an_exception(self):
        cid = self.verified()
        with captured_logs() as lines, self.patch_find(side_effect=RuntimeError(CANARY)):
            text_ctx = self.context(cid)
        self.assertEqual(text_ctx['transits'], UNAVAILABLE_FAILED)
        self.assertEqual(text_ctx['limitations'], VERIFIED_LIMITATIONS)
        self.assertNotIn('КАНАРЕЙКА', compact(text_ctx))
        self.assertEqual(len(transit_lines(lines)), 1)

    def test_tc9_slow_calculation_logs_one_slow_line(self):
        cid = self.verified()
        real = self.transits.find_transits

        def slow(*args, **kwargs):
            time.sleep(0.75)              # > 500 ms on any clock resolution; not a race, only a lower bound
            return real(*args, **kwargs)
        with captured_logs() as lines, self.patch_find(side_effect=slow):
            _, block = self.block(cid)
        self.assertEqual(block['status'], 'ok')
        found = transit_lines(lines)
        self.assertEqual(len(found), 1, found)
        self.assertRegex(found[0], SLOW_LINE)
        self.assertGreaterEqual(int(re.search(r'duration_ms=(\d+)', found[0])[1]), 500)
        self.assertIn(int(re.search(r'rows=(\d+)', found[0])[1]),
                      {len(block['rows']), len(block['rows']) + block['omitted']})
        with captured_logs() as lines:
            self.block(cid)
        self.assertEqual(transit_lines(lines), [])


class DensePathTests(TransitContextBase):
    def test_tf1_dense_chart_stays_within_the_call_budget(self):
        """12 points along Jupiter's path 105-167°: ≤ 15 000 calc_ut calls; the time is printed."""
        natal = {point: 105 + i * (167 - 105) / 11 for i, point in enumerate(POINTS)}
        hz = self.transits.horizon(KYIV, NOW)
        real, calls = swisseph.calc_ut, [0]

        def counting(*args, **kwargs):
            calls[0] += 1
            return real(*args, **kwargs)
        with patch.object(swisseph, 'calc_ut', counting):
            started = time.monotonic()
            rows = self.transits.find_transits(natal, hz)
            elapsed = time.monotonic() - started
        print(f'\nTF1 dense chart: {len(rows)} transits, {calls[0]} calc_ut calls, {elapsed * 1000:.0f} ms')
        self.assertLessEqual(calls[0], 15000)
        self.assertGreater(len(rows), 10)
        self.assertTrue(all(len(t.exact) <= 3 for t in rows))


if __name__ == '__main__':
    unittest.main()
