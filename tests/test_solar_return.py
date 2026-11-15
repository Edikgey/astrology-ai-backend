"""SOLAR-1-be API regression SR1, SR2 and SR6 (tasks/SOLAR-1/DESIGN.md sections 2, 3, 4, 7): GET /natal-chart/{chart_id}/solar-return
and modules.solar_return.strongest.

Real application, isolated SQLite (the fixtures of tests/test_my_charts.py), synthetic data only. The only clock is
modules.transits.server_now, patched here. No network, no .secrets, no OpenAI: the endpoint must not touch the AI chat.
The accuracy of the calculation itself is checked against JPL in tests/test_solar_return_jpl.py.

Every test here is red on the base commit because modules.solar_return and the route do not exist yet: the module is imported
inside the tests (never at file level) so that each test fails on its own.
"""
import logging
import random
import re
import time
import unittest
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
from unittest.mock import patch

import swisseph

import test_chat_quality as cq
import test_solar_return_jpl as J
from database.queries import ChartData, GPTMessage, GPTUsage, NatalChart
from modules import transits

UTC = timezone.utc
BIRTH_UTC = datetime(2000, 1, 2, 10, 0)           # naive UTC, as stored
CHART_ZONE = 'Pacific/Auckland'                   # deliberately unlike every zone the tests ask for
CANARY = 'КАНАРЕЙКА-7781 1990-05-03'
# Texts of DESIGN section 4 and of the existing endpoints, copied literally: a changed text must fail here.
CHART_NOT_FOUND = 'Карта не найдена. Откройте её из раздела «Мои карты» или постройте новую.'
CHART_FORBIDDEN = 'Нет доступа к этой карте. Откройте свою карту из раздела «Мои карты».'
CHART_DATA_MISSING = 'Расчётные данные карты отсутствуют. Обратитесь в поддержку для восстановления карты.'
SOLAR_LEGACY_CHART = ('Карту года для этой карты не построить: она создана в старой версии Lunaria, где часовой пояс '
                      'рождения не проверялся. Постройте карту заново, выбрав место рождения из подсказок, и откройте '
                      '«Мой год» для новой карты.')
SOLAR_UNAVAILABLE = 'Сейчас не получилось рассчитать карту года. Попробуйте ещё раз через несколько минут.'
FAILED_LINE = re.compile(r'^Solar return outcome=failed reason=(ephemeris_error|missing_saved_positions|unclassified) '
                         r'duration_ms=\d+$')
SLOW_LINE = re.compile(r'^Solar return outcome=slow duration_ms=\d+$')
KEYS = {'timezone', 'start', 'next', 'ascendant_sign', 'sun_house', 'moon_sign', 'moon_house', 'aspects'}
# Case S1 of DESIGN section 5 (JPL reference moments, year chart literals), the example of DESIGN section 3.
EXPECTED_S1 = {
    'timezone': 'Europe/Kyiv',
    'start': {'date': '2027-03-10', 'time': '11:30'}, 'next': {'date': '2028-03-09', 'time': '17:19'},
    'ascendant_sign': 3, 'sun_house': 10, 'moon_sign': 0, 'moon_house': 10,
    'aspects': [{'a': '☿', 'aspect': '☍', 'b': '♂'}, {'a': '☽', 'aspect': '☌', 'b': '♄'}, {'a': '☽', 'aspect': '□', 'b': 'AS'}],
}
LEAKS = ('КАНАРЕЙКА', '7781', '1990-05-03', 'ephemeris file missing', 'Traceback')


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


def solar_lines(lines):
    return [line for line in lines if line.startswith('Solar return')]


class SolarApiBase(cq.ChatQualityBase):
    def setUp(self):
        super().setUp()
        J.require_solar_return(self)                # red on base: "modules.solar_return is missing"
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.now = self.stack.enter_context(patch.object(transits, 'server_now', return_value=J.CASES['S1']['now']))

    def freeze(self, case):
        self.now.return_value = J.CASES[case]['now']

    def verified(self, case='S1', bodies=None, timezone=CHART_ZONE, **fields):
        """A chart owned by user 1 with the saved Sun and the place of the reference case; the clock is the case's `now`."""
        spec = J.CASES[case]
        self.freeze(case)
        cid = self.chart()
        bodies = {'☉': cq.body('☉', spec['natal'])} if bodies is None else bodies
        self.store(cid, bodies, birth_utc=BIRTH_UTC, timezone=timezone, lat=spec['lat'], lon=spec['lon'], **fields)
        return cid

    def get(self, cid, headers='owner', **params):
        headers = self.owner if headers == 'owner' else headers
        return self.client.get(f'/natal-chart/{cid}/solar-return', headers=headers, params=params or None)

    def rows(self):
        with self.sessions() as db:
            return (db.query(GPTUsage).count(), db.query(GPTMessage).count(), db.query(NatalChart).count(),
                    [row.bodies_for_circle for row in db.query(ChartData).order_by(ChartData.chart_id)])


class ContractAndRefusalTests(SolarApiBase):
    """SR1: contract keys, refusals with their literal texts, one closed log line, no AI, no writes, no network."""

    def test_sr1_success_has_exactly_the_contract_keys_and_the_reference_values(self):
        cid = self.verified()
        before = self.rows()
        response = self.get(cid, tz='Europe/Kyiv')
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(set(body), KEYS)
        self.assertEqual(set(body['start']), {'date', 'time'})
        self.assertEqual(set(body['next']), {'date', 'time'})
        self.assertTrue(all(set(row) == {'a', 'aspect', 'b'} for row in body['aspects']))
        self.assertTrue(all(isinstance(body[key], int) for key in ('ascendant_sign', 'sun_house', 'moon_sign', 'moon_house')))
        self.assertTrue(re.fullmatch(r'\d{2}:\d{2}', body['start']['time']) and re.fullmatch(r'\d{2}:\d{2}', body['next']['time']))
        self.assertEqual(body, EXPECTED_S1)           # case S1 of DESIGN section 5, JPL reference moments
        self.assertEqual(self.rows(), before)         # no GPTUsage, no GPTMessage, no chart row, saved data unchanged

    def test_sr1_the_year_does_not_need_the_network(self):
        """DESIGN section 2: no Horizons, no asteroids; the calculation is Swiss Ephemeris only."""
        cid = self.verified()
        with patch('modules.ephemeris.Horizons', side_effect=AssertionError('the network must not be used')):
            response = self.get(cid, tz='Europe/Kyiv')
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), EXPECTED_S1)

    def test_sr1_authentication_and_ownership_are_decided_by_the_server(self):
        own = self.verified()
        foreign = self.chart(user_id=2)
        guest_chart = self.chart(user_id=None, session_token=self.guest_token)
        orphan = self.chart(user_id=None)
        for headers in ({}, self.guest, {'Authorization': 'Bearer invalid'}):
            with self.subTest(headers=list(headers)):
                self.assertEqual(self.get(own, headers=headers).status_code, 401)
        missing = self.get(99999)
        self.assertEqual((missing.status_code, missing.json()['detail']), (404, CHART_NOT_FOUND))
        for cid in (foreign, guest_chart, orphan):
            with self.subTest(chart=cid):
                response = self.get(cid)
                self.assertEqual((response.status_code, response.json()['detail']), (403, CHART_FORBIDDEN))
        other = self.client.get(f'/natal-chart/{own}/solar-return', headers=self.other)
        self.assertEqual((other.status_code, other.json()['detail']), (403, CHART_FORBIDDEN))

    def test_sr1_legacy_chart_is_refused_without_any_ephemeris_call(self):
        calls = []
        real_cross, real_calc = swisseph.solcross_ut, swisseph.calc_ut

        def recording_cross(*args, **kwargs):
            calls.append(('solcross_ut', args))
            return real_cross(*args, **kwargs)

        def recording_calc(*args, **kwargs):
            calls.append(('calc_ut', args))
            return real_calc(*args, **kwargs)

        variants = ({}, {'birth_utc': BIRTH_UTC, 'timezone': None}, {'birth_utc': BIRTH_UTC, 'timezone': ''},
                    {'birth_utc': BIRTH_UTC, 'timezone': 'Mars/Olympus_Mons'}, {'birth_utc': None, 'timezone': 'Europe/Kyiv'})
        for fields in variants:
            with self.subTest(fields=fields):
                cid = self.chart()
                self.store(cid, {'☉': cq.body('☉', J.CASES['S1']['natal'])}, **fields)
                with patch.object(swisseph, 'solcross_ut', recording_cross), patch.object(swisseph, 'calc_ut', recording_calc):
                    response = self.get(cid, tz='Europe/Kyiv')
                self.assertEqual((response.status_code, response.json()['detail']), (422, SOLAR_LEGACY_CHART))
        self.assertEqual(calls, [])
        # A legacy chart is refused as legacy even if its saved data is also gone (DESIGN section 4 order).
        cid = self.chart(related=False)
        response = self.get(cid)
        self.assertEqual((response.status_code, response.json()['detail']), (422, SOLAR_LEGACY_CHART))

    def test_sr1_missing_saved_sun_is_a_404_with_one_closed_log_line(self):
        no_data = self.chart(related=False)
        with self.sessions() as db:
            chart = db.get(NatalChart, no_data)
            chart.birth_utc, chart.timezone = BIRTH_UTC, CHART_ZONE
            db.commit()
        natal = J.CASES['S1']['natal']
        broken = {label: {'☉': cq.body('☉', natal)} for label in ('beyond 360', 'negative', 'text', 'none')}
        broken['beyond 360']['☉']['degree'] = 360.0
        broken['negative']['☉']['degree'] = -1.0
        broken['text']['☉']['degree'] = 'x'
        broken['none']['☉'] = None
        cases = [('no ChartData', no_data), ('empty', self.verified(bodies={})),
                 ('other points fine, no Sun', self.verified(bodies={symbol: cq.body(symbol, 10.0 + i) for i, symbol in
                                                                      enumerate(('☽', '☿', '♀', '♂', 'AS', 'MC'))}))]
        cases += [(label, self.verified(bodies=bodies)) for label, bodies in broken.items()]
        for label, cid in cases:
            with self.subTest(case=label), captured_logs() as lines:
                response = self.get(cid, tz='Europe/Kyiv')
                self.assertEqual((response.status_code, response.json()['detail']), (404, CHART_DATA_MISSING))
                found = solar_lines(lines)
                self.assertEqual(len(found), 1, found)
                self.assertRegex(found[0], FAILED_LINE)
                self.assertIn('reason=missing_saved_positions ', found[0])

    def test_sr1_calculation_failures_are_a_503_with_one_closed_log_line_and_no_leak(self):
        cid = self.verified()
        for error, reason in ((swisseph.Error('ephemeris file missing'), 'ephemeris_error'),
                              (RuntimeError(CANARY), 'unclassified')):
            with self.subTest(error=type(error).__name__):
                before = self.rows()
                with captured_logs() as lines, patch.object(swisseph, 'solcross_ut', side_effect=error):
                    response = self.get(cid, tz='Europe/Kyiv')
                self.assertEqual(response.status_code, 503, response.text)
                self.assertEqual(response.json()['detail'], SOLAR_UNAVAILABLE)
                for leak in LEAKS:
                    self.assertNotIn(leak, response.text)
                found = solar_lines(lines)
                self.assertEqual(len(found), 1, found)                     # exactly one line per request
                self.assertRegex(found[0], FAILED_LINE)
                self.assertIn(f'reason={reason} ', found[0])
                for line in lines:                                          # nothing else leaks either
                    for leak in LEAKS + ('Europe/Kyiv', '2027-03-10', '349.58'):
                        self.assertNotIn(leak, line)
                self.assertEqual(self.rows(), before)
        with captured_logs() as lines:                                      # recovery: the next request is fine
            self.assertEqual(self.get(cid, tz='Europe/Kyiv').status_code, 200)
        self.assertEqual(solar_lines(lines), [])                            # a fast success logs nothing

    def test_sr1_slow_success_logs_one_slow_line(self):
        cid = self.verified()
        real = swisseph.solcross_ut
        state = {'slept': False}

        def slow_once(*args, **kwargs):
            if not state['slept']:
                state['slept'] = True
                time.sleep(0.6)
            return real(*args, **kwargs)

        with captured_logs() as lines, patch.object(swisseph, 'solcross_ut', slow_once):
            response = self.get(cid, tz='Europe/Kyiv')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), EXPECTED_S1)
        found = solar_lines(lines)
        self.assertEqual(len(found), 1, found)
        self.assertRegex(found[0], SLOW_LINE)

    def test_sr1_other_users_own_chart_works_and_the_ai_chat_is_untouched(self):
        cid = self.chart(user_id=2)
        spec = J.CASES['S1']
        self.store(cid, {'☉': cq.body('☉', spec['natal'])}, birth_utc=BIRTH_UTC, timezone='Europe/Kyiv',
                   lat=spec['lat'], lon=spec['lon'])
        with self.sessions() as db:
            before = (db.query(GPTUsage).count(), db.query(GPTMessage).count())
        for _ in range(3):                                                  # no quota, no rate limit on reading
            response = self.client.get(f'/natal-chart/{cid}/solar-return', headers=self.other, params={'tz': 'Europe/Kyiv'})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json(), EXPECTED_S1)
        with self.sessions() as db:
            self.assertEqual((db.query(GPTUsage).count(), db.query(GPTMessage).count()), before)


class LocalTimeTests(SolarApiBase):
    """SR2: the two moments are one instant each; their local date and time follow the device zone (`tz`), else the chart's zone."""

    def test_sr2_device_zone_decides_the_local_strings(self):
        cid = self.verified('S4')
        kathmandu = self.get(cid, tz='Asia/Kathmandu').json()
        new_york = self.get(cid, tz='America/New_York').json()
        self.assertEqual(kathmandu['timezone'], 'Asia/Kathmandu')
        self.assertEqual((kathmandu['start'], kathmandu['next']), ({'date': '2027-05-21', 'time': '00:35'},
                                                                  {'date': '2028-05-20', 'time': '06:27'}))
        self.assertEqual(new_york['timezone'], 'America/New_York')
        self.assertEqual((new_york['start'], new_york['next']), ({'date': '2027-05-20', 'time': '14:50'},
                                                                {'date': '2028-05-19', 'time': '20:42'}))
        # One instant, three zones: the same year chart whatever the zone.
        for key in ('ascendant_sign', 'sun_house', 'moon_sign', 'moon_house', 'aspects'):
            self.assertEqual(kathmandu[key], new_york[key])

    def test_sr2_the_new_year_changes_the_local_date(self):
        cid = self.verified('S5')
        kyiv = self.get(cid, tz='Europe/Kyiv').json()
        utc_zone = self.get(cid, tz='UTC').json()
        self.assertEqual((kyiv['timezone'], kyiv['start']), ('Europe/Kyiv', {'date': '2027-01-01', 'time': '01:00'}))
        self.assertEqual(kyiv['next']['date'], '2028-01-01')                # 06:43:59: the minute is not asserted (JPL boundary)
        self.assertEqual((utc_zone['timezone'], utc_zone['start']), ('UTC', {'date': '2026-12-31', 'time': '23:00'}))

    def test_sr2_southern_hemisphere_reference_case(self):
        cid = self.verified('S7')
        body = self.get(cid, tz='America/Argentina/Buenos_Aires').json()
        self.assertEqual(body['start'], {'date': '2027-08-14', 'time': '23:37'})
        self.assertEqual(body['next'], {'date': '2028-08-14', 'time': '05:32'})

    def test_sr2_missing_or_unusable_tz_falls_back_to_the_chart_zone_without_any_422(self):
        cid = self.verified('S4', timezone='America/New_York')
        for label, params in (('absent', {}), ('empty', {'tz': ''}), ('unknown', {'tz': 'Mars/Olympus'}),
                              ('traversal', {'tz': '../etc/passwd'}), ('65 chars', {'tz': 'A' * 65}),
                              ('not ascii', {'tz': 'Европа/Киев'}), ('spaces', {'tz': 'Europe/Kyiv; drop table'})):
            with self.subTest(tz=label):
                response = self.get(cid, **params)
                self.assertEqual(response.status_code, 200, response.text)
                body = response.json()
                self.assertEqual(body['timezone'], 'America/New_York')          # the zone the moments were written in
                self.assertEqual(body['start'], {'date': '2027-05-20', 'time': '14:50'})
                self.assertEqual(body['next'], {'date': '2028-05-19', 'time': '20:42'})

    def test_sr2_the_zone_is_chosen_per_request_and_not_remembered(self):
        cid = self.verified('S4', timezone='America/New_York')
        for tz, day in (('Asia/Kathmandu', '2027-05-21'), (None, '2027-05-20'), ('Asia/Kathmandu', '2027-05-21')):
            params = {} if tz is None else {'tz': tz}
            self.assertEqual(self.get(cid, **params).json()['start']['date'], day)


# --- SR6: the choice of aspects, no ephemeris (DESIGN section 2, `strongest`) -------------------------------------------
ORDER = ('☉', '☽', '☿', '♀', '♂', '♃', '♄', '♅', '♆', '♇', 'AS', 'MC')
FAST = ('☉', '☽', '☿', '♀', '♂', 'AS', 'MC')
ASPECTS = (('☌', 0), ('⚹', 60), ('□', 90), ('△', 120), ('☍', 180))
LIMIT = 8.0


def chart(**longitudes):
    """A full set of twelve points from the literal longitudes (symbols that are not identifiers go through `dict`)."""
    return {symbol: float(value) for symbol, value in longitudes.items()}


def reference(points):
    """The DESIGN rule written out the long way: (orb, a, aspect, b) sorted by orb, then point order; the first three."""
    rows = []
    for i, a in enumerate(ORDER):
        for j in range(i + 1, len(ORDER)):
            b = ORDER[j]
            if {a, b} == {'AS', 'MC'} or not (a in FAST or b in FAST):
                continue
            difference = abs(points[a] - points[b])
            separation = min(difference, 360 - difference)
            for symbol, angle in ASPECTS:
                orb = abs(separation - angle)
                if orb <= LIMIT:
                    rows.append((orb, i, j, a, symbol, b))
    rows.sort(key=lambda row: row[:3])
    return [(a, symbol, b) for _, _, _, a, symbol, b in rows[:3]]


# Literal cases: (points, the three strongest). Every orb is a whole or half number of degrees, so equal orbs are exactly equal.
BASE_TWO = {'☉': 144.0, '☽': 146.0, '☿': 69.0, '♀': 193.0, '♂': 171.0, '♃': 0.0, '♄': 0.0, '♅': 0.0, '♆': 0.0, '♇': 0.0,
            'AS': 39.0, 'MC': 221.0}
SR6_CASES = {
    # Orbs 0, 3 and 2 by pair order would be (♂ MC), (☉ ♀), (☿ ♆); by orb they are 0 < 2 < 3: here already the order of the pairs
    # is not the order of the answer for ☿ ♆ against ☉ ♀, and a fourth and fifth aspect (orb 4) are cut off.
    'by orb, cut at three': (
        {'☉': 180.0, '☽': 43.0, '☿': 350.0, '♀': 242.0, '♂': 39.0, '♃': 213.0, '♄': 15.0, '♅': 255.0, '♆': 293.0, '♇': 7.0,
         'AS': 319.0, 'MC': 339.0},
        [('♂', '⚹', 'MC'), ('☉', '⚹', '♀'), ('☿', '⚹', '♆')]),
    # Equal orb 1: the same first point, then the order of the second point (AS before MC).
    'equal orb, same first point': (
        {'☉': 188.0, '☽': 174.0, '☿': 313.0, '♀': 177.0, '♂': 162.0, '♃': 140.0, '♄': 49.0, '♅': 45.0, '♆': 94.0, '♇': 25.0,
         'AS': 335.0, 'MC': 95.0},
        [('♆', '△', 'AS'), ('♆', '☌', 'MC'), ('☿', '□', '♅')]),
    # Equal orb 1: ☉ comes before ♂ although ♆ comes after ♅: the first point decides, then the second.
    'equal orb, earlier first point wins': (
        {'☉': 50.0, '☽': 276.0, '☿': 310.0, '♀': 108.0, '♂': 84.0, '♃': 268.0, '♄': 81.0, '♅': 175.0, '♆': 111.0, '♇': 211.0,
         'AS': 279.0, 'MC': 183.0},
        [('☉', '⚹', '♆'), ('♂', '□', '♅'), ('☉', '⚹', '♀')]),
    # Fewer than three: the slow planets stand on one point (ten conjunctions of orb 0 between ♃ ... ♇) and AS ☍ MC has orb 2:
    # none of them may appear; only two pairs with a fast planet are left.
    'two aspects, slow pairs and AS MC left out': (BASE_TWO, [('☉', '☌', '☽'), ('☿', '△', '♀')]),
    'half degree orb sorts before whole degrees': (dict(BASE_TWO, **{'♀': 190.5}), [('☿', '△', '♀'), ('☉', '☌', '☽')]),
    # The limit is 8 degrees and inclusive: orb 8.0 stays, orb 8.5 goes.
    'orb exactly 8 stays': (dict(BASE_TWO, **{'♀': 197.0}), [('☉', '☌', '☽'), ('☉', '⚹', '♀'), ('☿', '△', '♀')]),
    'orb 8.5 goes': (dict(BASE_TWO, **{'♀': 197.5}), [('☉', '☌', '☽'), ('☉', '⚹', '♀')]),
}


class StrongestAspectsTests(unittest.TestCase):
    def setUp(self):
        self.solar = J.require_solar_return(self)

    def strongest(self, points):
        return [tuple(row) for row in self.solar.strongest(points)]

    def test_sr6_literal_cases(self):
        for name, (points, expected) in SR6_CASES.items():
            with self.subTest(case=name):
                self.assertEqual(self.strongest(points), expected)
                self.assertEqual(reference(points), expected, 'the literal agrees with the long-hand rule')

    def test_sr6_the_order_of_the_input_does_not_matter(self):
        for name, (points, expected) in SR6_CASES.items():
            with self.subTest(case=name):
                shuffled = list(points.items())
                random.Random(3).shuffle(shuffled)
                self.assertEqual(self.strongest(dict(shuffled)), expected)

    def test_sr6_random_charts_agree_with_the_long_hand_rule(self):
        rng = random.Random(20261115)
        for index in range(300):
            points = {symbol: round(rng.uniform(0, 360), 4) for symbol in ORDER}
            with self.subTest(case=index):
                result = self.strongest(points)
                self.assertEqual(result, reference(points))
                self.assertLessEqual(len(result), 3)

    def test_sr6_does_not_change_its_input(self):
        points, _ = next(iter(SR6_CASES.values()))
        before = dict(points)
        self.solar.strongest(points)
        self.assertEqual(points, before)


if __name__ == '__main__':
    unittest.main()
