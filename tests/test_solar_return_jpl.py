"""SOLAR-1-be accuracy regression SR3-SR5 against NASA JPL Horizons (DECISIONS 26, 30; tasks/SOLAR-1/DESIGN.md sections 2, 5, 7).

`modules.solar_return` is called through the DESIGN section 2 contract only:
    calculate(sun, lat, lon, now) -> SolarYear with start, next (aware UTC datetimes), points {symbol: longitude},
    ascendant_sign, sun_house, moon_sign, moon_house, aspects [(a, aspect symbol, b)] (at most three).
The tables in tests/fixtures/horizons/ (Sun 10_1d: 1 day, 2025-06-01 .. 2029-01-01; Mercury, Venus, Mars 199/299/499: 6 h,
2026-11-01 .. 2028-01-01; Moon 301: 6 h, same range; Jupiter .. Pluto 599 .. 999: 1 day, 2025-11-01 .. 2028-01-31) were
downloaded once. Nothing here uses the network or Swiss Ephemeris except the module under test: the reference reads the tables
by 4-point Lagrange interpolation and finds the Sun's return by bisection, a method deliberately different from the module's
(`swisseph.solcross_ut`).

The fixture and reference tests (HorizonsFixtureTests, ReferenceLiteralTests) are green on the base commit; the tests that call
modules.solar_return are red there because the module does not exist yet.
"""
import hashlib
import importlib
import inspect
import math
import random
import re
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import numpy as np
import swisseph

from modules.ephemeris import Ephemeris

UTC = timezone.utc
FIXTURES = Path(__file__).resolve().parent / 'fixtures' / 'horizons'
ARCSEC = 1 / 3600
MOMENT_TOLERANCE = 0.2 * ARCSEC          # |lambda_Sun_JPL(t) - natal| at the module's moment (DESIGN section 5)
POSITION_TOLERANCE = 2 * ARCSEC          # ten longitudes of the year chart against JPL (DECISIONS 26)
BOUNDARY = 5                             # seconds: a JPL moment this close to a minute boundary does not check "HH:MM"
SHA256 = {'10_1d': '62d2e3fe303948e5a58468df2081ba2916caf2d7a771e8357813ee198bfe170e',
          '199': 'b280c4ddcca217c4e559438fd2093678c1034c66242a1d3e24bf210ce5292179',
          '299': '63f34b3d9d203ce68eccf13c89bc82cee49b7f41a5bb71c8d96b31b09ae896c9',
          '499': '9d588a12fa9fc4760c9d3a96f8e62ee248f66f4fbcb0220082e7a7fd3f352b1e'}
MONTHS = {m: i + 1 for i, m in enumerate(('Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
                                          'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'))}
ROW = re.compile(r'^ (\d{4})-([A-Z][a-z]{2})-(\d{2}) (\d{2}):(\d{2})\s+(.+)$')
BODIES = ('☉', '☽', '☿', '♀', '♂', '♃', '♄', '♅', '♆', '♇')


def utc(*parts):
    return datetime(*parts, tzinfo=UTC)


def as_utc(moment):
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)


def wrap(value):
    """Signed difference in (-180, 180]."""
    return -((-value + 180) % 360 - 180)


def raw_bytes(identifier):
    return (FIXTURES / f'{identifier}.txt').read_bytes()


def raw_text(identifier):
    return (FIXTURES / f'{identifier}.txt').read_text(encoding='utf-8')     # universal newlines: CRLF or LF


class Table:
    """One Horizons table: UTC nodes at a fixed step and the unwrapped ecliptic longitude (degrees)."""

    def __init__(self, identifier, step_hours, lon_index):
        body = raw_text(identifier).split('$$SOE', 1)[1].split('$$EOE', 1)[0]
        stamps, lon = [], []
        for line in body.splitlines():
            if not line.strip():
                continue
            match = ROW.match(line.rstrip())
            if not match:
                raise AssertionError(f'unexpected Horizons row in {identifier}: {line!r}')
            stamps.append(utc(int(match[1]), MONTHS[match[2]], int(match[3]), int(match[4]), int(match[5])))
            lon.append(float(match[6].split()[lon_index]))
        self.identifier, self.stamps, self.step = identifier, stamps, step_hours * 3600
        self.epoch = stamps[0]
        self.lon = np.unwrap(np.array(lon), period=360.0).tolist()

    def seconds(self, moment):
        return (as_utc(moment) - self.epoch).total_seconds()

    def valid_range(self):
        """Seconds (from the epoch) where the 4-point interpolation is defined."""
        return self.step, (len(self.lon) - 3) * self.step

    def unwrapped_s(self, sec):
        t = sec / self.step
        i = math.floor(t)
        if i < 1 or i > len(self.lon) - 3:
            raise ValueError(f'moment outside the interpolation range of the JPL table {self.identifier}')
        u = t - i
        y0, y1, y2, y3 = self.lon[i - 1:i + 3]
        return (-u * (u - 1) * (u - 2) / 6 * y0 + (u + 1) * (u - 1) * (u - 2) / 2 * y1
                - (u + 1) * u * (u - 2) / 2 * y2 + (u + 1) * u * (u - 1) / 6 * y3)

    def unwrapped(self, moment):
        return self.unwrapped_s(self.seconds(moment))

    def at(self, moment):
        return self.unwrapped(moment) % 360


_TABLES = {}


def tables():
    if not _TABLES:
        _TABLES.update({
            '☉': Table('10_1d', 24, 0), '☽': Table('301', 6, 1), '☿': Table('199', 6, 0), '♀': Table('299', 6, 0),
            '♂': Table('499', 6, 0), '♃': Table('599', 24, 0), '♄': Table('699', 24, 0), '♅': Table('799', 24, 0),
            '♆': Table('899', 24, 0), '♇': Table('999', 24, 0)})
    return _TABLES


def sun(moment):
    return tables()['☉'].at(moment)


def crossing(value):
    """UTC moment where the unwrapped JPL Sun longitude equals `value` (degrees): bisection, no Swiss Ephemeris."""
    table = tables()['☉']
    lo, hi = table.valid_range()
    if not table.unwrapped_s(lo) <= value <= table.unwrapped_s(hi):
        raise ValueError('target outside the Sun table')
    while hi - lo > 0.001:
        mid = (lo + hi) / 2
        if table.unwrapped_s(mid) < value:
            lo = mid
        else:
            hi = mid
    return table.epoch + timedelta(seconds=(lo + hi) / 2)


def reference_year(natal, now):
    """(start, next): the JPL Sun returns to `natal` around `now`; start < now <= next (DESIGN section 2)."""
    value = natal + 360 * math.ceil((tables()['☉'].unwrapped(now) - natal) / 360)
    return crossing(value - 360), crossing(value)


def residual(moment, natal):
    """|lambda_Sun_JPL(moment) - natal| in degrees."""
    return abs(wrap(sun(moment) - natal))


def stamp(text):
    """'2027-03-10 09:30:19.97' (UTC, as printed in DESIGN section 5) -> aware datetime."""
    head, _, fraction = text.partition('.')
    return datetime.strptime(head, '%Y-%m-%d %H:%M:%S').replace(tzinfo=UTC) + timedelta(seconds=float('0.' + fraction or 0))


def local_text(moment, zone_name):
    return as_utc(moment).astimezone(ZoneInfo(zone_name)).strftime('%Y-%m-%d %H:%M')


def minute_safe(moment, zone_name):
    """True when the moment is at least BOUNDARY seconds from a minute boundary: "HH:MM" is then unambiguous."""
    local = as_utc(moment).astimezone(ZoneInfo(zone_name))
    sec = local.second + local.microsecond / 1e6
    return min(sec, 60 - sec) >= BOUNDARY


# --- DESIGN section 5: reference cases S1-S7 ------------------------------------------------------------------------------
# natal sun, place, now (UTC), JPL moments (UTC), local "YYYY-MM-DD HH:MM" of start and next per zone (None: the JPL moment sits
# within 5 s of a minute boundary, so only the residual is compared).
CASES = {
    'S1': dict(natal=349.581254, lat=50.4501, lon=30.5234, now=utc(2027, 6, 1, 12, 0),
               start='2027-03-10 09:30:19.97', next='2028-03-09 15:19:23.45',
               local={'Europe/Kyiv': ('2027-03-10 11:30', '2028-03-09 17:19')}),
    'S2': dict(natal=349.581254, lat=50.4501, lon=30.5234, now=utc(2027, 3, 10, 9, 29, 20),
               start='2026-03-10 03:54:03.18', next='2027-03-10 09:30:19.97',
               local={'Europe/Kyiv': (None, '2027-03-10 11:30')}),
    'S3': dict(natal=349.581254, lat=50.4501, lon=30.5234, now=utc(2027, 3, 10, 9, 31, 20),
               start='2027-03-10 09:30:19.97', next='2028-03-09 15:19:23.45',
               local={'Europe/Kyiv': ('2027-03-10 11:30', '2028-03-09 17:19')}),
    'S4': dict(natal=59.540420, lat=27.7172, lon=85.3240, now=utc(2027, 7, 1, 0, 0),
               start='2027-05-20 18:50:19.96', next='2028-05-20 00:42:16.90',
               local={'Asia/Kathmandu': ('2027-05-21 00:35', '2028-05-20 06:27'),
                      'America/New_York': ('2027-05-20 14:50', '2028-05-19 20:42')}),
    'S5': dict(natal=280.278096, lat=50.4501, lon=30.5234, now=utc(2027, 1, 15, 0, 0),
               start='2026-12-31 23:00:19.99', next='2028-01-01 04:43:59.14',
               local={'Europe/Kyiv': ('2027-01-01 01:00', None)}),
    'S6': dict(natal=359.734837, lat=55.7558, lon=37.6173, now=utc(2027, 4, 1, 0, 0),
               start='2027-03-20 14:00:20.03', next='2028-03-19 19:52:58.92',
               local={'Europe/Moscow': ('2027-03-20 17:00', None)}),
    'S7': dict(natal=142.081356, lat=-34.6037, lon=-58.3816, now=utc(2027, 9, 1, 0, 0),
               start='2027-08-15 02:37:20.00', next='2028-08-14 08:32:11.21',
               local={'America/Argentina/Buenos_Aires': ('2027-08-14 23:37', '2028-08-14 05:32')}),
}
# Year chart at `start` (DESIGN section 5): ascendant sign, Sun house, Moon sign, Moon house, three strongest aspects.
CHART_LITERALS = {
    'S1': (3, 10, 0, 10, [('☿', '☍', '♂'), ('☽', '☌', '♄'), ('☽', '□', 'AS')]),
    'S4': (10, 3, 8, 9, [('♀', '□', '♇'), ('♇', '⚹', 'MC'), ('☽', '□', '♂')]),
    'S5': (6, 3, 6, 1, [('☽', '□', 'MC'), ('☿', '△', '♂'), ('☉', '△', '♂')]),
    'S6': (5, 7, 5, 12, [('☿', '□', '♅'), ('♇', '△', 'MC'), ('♀', '☍', '♂')]),
    'S7': (0, 4, 9, 9, [('☽', '□', '♄'), ('☽', '□', 'AS'), ('♄', '☌', 'AS')]),
}
# Agreement with the natal chart (SC, no JPL): place (lat, lon) and the birth moment in UTC.
BIRTHS = {'Kyiv': (50.4501, 30.5234, utc(1990, 5, 3, 6, 15)),
          'Buenos Aires': (-34.6037, -58.3816, utc(1990, 5, 3, 6, 15)),
          'Reykjavik': (64.1466, -21.9426, utc(1985, 1, 20, 22, 40))}


def random_cases(count=300, seed=20261115):
    """(natal sun, lat, lon, now): the natal Sun is the JPL Sun at a random t0, `now` is 1 s .. 364.5 days after t0 (DESIGN section 5)."""
    rng = random.Random(seed)
    first, last = utc(2026, 11, 1, 18, 0), utc(2027, 12, 30, 12, 0)
    span = (last - first).total_seconds()
    cases = []
    for _ in range(count):
        t0 = first + timedelta(seconds=rng.uniform(0, span))
        natal = round(sun(t0), 6)
        now = t0 + timedelta(seconds=rng.uniform(1, 364.5 * 86400))
        cases.append((natal, rng.uniform(-60, 60), rng.uniform(-180, 180), now))
    return cases


def require_solar_return(test):
    try:
        return importlib.import_module('modules.solar_return')
    except ModuleNotFoundError as error:
        if error.name != 'modules.solar_return':
            raise
        test.fail('modules.solar_return is missing: the DESIGN section 2 contract is not implemented yet')


class HorizonsFixtureTests(unittest.TestCase):
    """Green on base: the four new tables are the unmodified one-time responses described in DESIGN section 5."""

    def test_fixtures_are_the_unmodified_downloads(self):
        for identifier, digest in SHA256.items():
            with self.subTest(identifier=identifier):
                lf_form = raw_bytes(identifier).replace(b'\r\n', b'\n')   # core.autocrlf=true: CRLF on disk
                self.assertEqual(hashlib.sha256(lf_form).hexdigest(), digest)

    def test_headers_and_requests(self):
        for identifier, command, start, stop, step, name in (
                ('10_1d', '10', '2025-06-01', '2029-01-01', '1 d', 'Sun (10)'),
                ('199', '199', '2026-11-01', '2028-01-01', '6 h', 'Mercury (199)'),
                ('299', '299', '2026-11-01', '2028-01-01', '6 h', 'Venus (299)'),
                ('499', '499', '2026-11-01', '2028-01-01', '6 h', 'Mars (499)')):
            with self.subTest(identifier=identifier):
                head, response = raw_text(identifier).split('# ---- response ----\n', 1)
                self.assertRegex(head, r'# Requested once \(UTC[^)]*\): 2026-11-15T17:16:0\dZ')
                for parameter in (f"COMMAND='{command}'", "CENTER='500@399'", f"START_TIME='{start}'", f"STOP_TIME='{stop}'",
                                  f"STEP_SIZE='{step}'", "QUANTITIES='31'", "ANG_FORMAT='DEG'", "EXTRA_PREC='YES'",
                                  "TIME_TYPE='UT'", "EPHEM_TYPE='OBSERVER'"):
                    self.assertIn(parameter, head)
                self.assertTrue(response.startswith('API VERSION: 1.2'))
                self.assertIn('API SOURCE: NASA/JPL Horizons API', response)
                self.assertIn(f'Target body name: {name}', response)
                self.assertIn('Center-site name: GEOCENTRIC', response)
                self.assertIn('ObsEcLon', response)

    def test_tables_are_complete_and_evenly_stepped(self):
        expected = {'☉': (1311, 24, utc(2025, 6, 1), utc(2029, 1, 1)), '☽': (1705, 6, utc(2026, 11, 1), utc(2028, 1, 1)),
                    '☿': (1705, 6, utc(2026, 11, 1), utc(2028, 1, 1)), '♀': (1705, 6, utc(2026, 11, 1), utc(2028, 1, 1)),
                    '♂': (1705, 6, utc(2026, 11, 1), utc(2028, 1, 1)), '♃': (None, 24, utc(2025, 11, 1), utc(2028, 1, 31))}
        for symbol, (rows, hours, first, last) in expected.items():
            with self.subTest(body=symbol):
                table = tables()[symbol]
                if rows is not None:
                    self.assertEqual(len(table.stamps), rows)
                self.assertEqual(table.stamps[0], first)
                self.assertEqual(table.stamps[-1], last)
                self.assertTrue(all(b - a == timedelta(hours=hours) for a, b in zip(table.stamps, table.stamps[1:])))
                self.assertLess(max(abs(b - a) for a, b in zip(table.lon, table.lon[1:])), 4.5 * hours / 6)   # no wrap left
        for symbol in '♄♅♆♇':
            with self.subTest(body=symbol):
                self.assertTrue(all(b - a == timedelta(days=1)
                                    for a, b in zip(tables()[symbol].stamps, tables()[symbol].stamps[1:])))
                self.assertEqual(tables()[symbol].stamps[0], utc(2025, 11, 1))

    def test_interpolation_reproduces_nodes(self):
        sun_table = tables()['☉']
        for k in (5, 300, 900, 1200):
            self.assertAlmostEqual(sun(sun_table.stamps[k]), sun_table.lon[k] % 360, delta=1e-9)


class ReferenceLiteralTests(unittest.TestCase):
    """Green on base: the DESIGN literals are re-derived from the tables by the independent reference."""

    def test_moments_match_the_design_table(self):
        for name, case in CASES.items():
            with self.subTest(case=name):
                start, nxt = reference_year(case['natal'], case['now'])
                self.assertAlmostEqual((start - stamp(case['start'])).total_seconds(), 0, delta=1.5)
                self.assertAlmostEqual((nxt - stamp(case['next'])).total_seconds(), 0, delta=1.5)
                self.assertTrue(start < case['now'] <= nxt)
                for zone_name, (local_start, local_next) in case['local'].items():
                    # The literal local strings are the literal UTC moments in the zone (a typo in either shows here).
                    for moment, literal in ((stamp(case['start']), local_start), (stamp(case['next']), local_next)):
                        if literal is not None:
                            self.assertEqual(local_text(moment, zone_name), literal)
                            self.assertTrue(minute_safe(moment, zone_name))
                        else:
                            self.assertFalse(minute_safe(moment, zone_name))

    def test_sun_returns_are_a_year_apart_and_the_natal_longitude_is_the_sun_at_that_moment(self):
        for name, case in CASES.items():
            with self.subTest(case=name):
                start, nxt = reference_year(case['natal'], case['now'])
                self.assertAlmostEqual((nxt - start).total_seconds() / 86400, 365.2422, delta=0.02)
                self.assertLessEqual(residual(start, case['natal']), 0.01 * ARCSEC)
                self.assertLessEqual(residual(nxt, case['natal']), 0.01 * ARCSEC)

    def test_boundary_cases_are_what_the_design_says(self):
        """S2 is one minute before the return, S3 one minute after: the two years either side of one moment."""
        before = reference_year(CASES['S2']['natal'], CASES['S2']['now'])
        after = reference_year(CASES['S3']['natal'], CASES['S3']['now'])
        self.assertEqual(before[0].year, 2026)
        self.assertEqual(before[1].year, after[0].year)
        self.assertAlmostEqual((before[1] - after[0]).total_seconds(), 0, delta=0.01)
        self.assertGreater(CASES['S6']['natal'], 359.7)                        # S6 sits next to 0 degrees Aries

    def test_random_cases_are_built_as_the_design_says(self):
        cases = random_cases()
        self.assertGreaterEqual(len(cases), 200)
        for natal, lat, lon, now in cases:
            self.assertTrue(0 <= natal < 360 and -60 <= lat <= 60 and -180 <= lon <= 180)
        start, nxt = reference_year(cases[0][0], cases[0][3])
        self.assertTrue(start < cases[0][3] <= nxt)


class SolarMomentTests(unittest.TestCase):
    """SR3 (red on base): the moment of the solar return against JPL."""

    def setUp(self):
        self.solar = require_solar_return(self)

    def calc(self, case):
        return self.solar.calculate(case['natal'], case['lat'], case['lon'], case['now'])

    def assert_moment(self, moment, natal, now_label):
        self.assertIsInstance(moment, datetime, now_label)
        self.assertIsNotNone(moment.tzinfo, 'start and next are aware UTC datetimes (DESIGN section 2)')
        self.assertLessEqual(residual(moment, natal), MOMENT_TOLERANCE, f'{now_label}: {moment}')

    def test_sr3_reference_cases_residual_and_local_minutes(self):
        for name, case in CASES.items():
            with self.subTest(case=name):
                result = self.calc(case)
                self.assert_moment(result.start, case['natal'], f'{name} start')
                self.assert_moment(result.next, case['natal'], f'{name} next')
                self.assertTrue(as_utc(result.start) < case['now'] <= as_utc(result.next))
                self.assertAlmostEqual((as_utc(result.start) - stamp(case['start'])).total_seconds(), 0, delta=6)
                self.assertAlmostEqual((as_utc(result.next) - stamp(case['next'])).total_seconds(), 0, delta=6)
                for zone_name, (local_start, local_next) in case['local'].items():
                    if local_start is not None:
                        self.assertEqual(local_text(result.start, zone_name), local_start, f'{name} {zone_name} start')
                    if local_next is not None:
                        self.assertEqual(local_text(result.next, zone_name), local_next, f'{name} {zone_name} next')

    def test_sr3_a_minute_before_and_after_the_return_are_the_year_before_and_after(self):
        before, after = self.calc(CASES['S2']), self.calc(CASES['S3'])
        self.assertEqual(as_utc(before.start).year, 2026)                      # the year that ended a minute ago
        self.assertTrue(as_utc(before.start) < CASES['S2']['now'] <= as_utc(before.next))
        self.assertTrue(as_utc(after.start) < CASES['S3']['now'] <= as_utc(after.next))
        self.assertEqual(as_utc(after.start).year, 2027)                       # the year that began a minute ago
        # One instant: the end of the old year is the start of the new one.
        self.assertAlmostEqual((as_utc(before.next) - as_utc(after.start)).total_seconds(), 0, delta=1)

    def test_sr3_the_return_next_to_zero_degrees_aries(self):
        result = self.calc(CASES['S6'])
        self.assertTrue(as_utc(result.start) < as_utc(result.next))
        self.assertEqual((as_utc(result.start).year, as_utc(result.start).month, as_utc(result.start).day), (2027, 3, 20))
        self.assertEqual((as_utc(result.next).year, as_utc(result.next).month, as_utc(result.next).day), (2028, 3, 19))

    def test_sr3_random_cases_agree_with_the_independent_pass(self):
        cases = random_cases()
        self.assertGreaterEqual(len(cases), 200)
        for index, (natal, lat, lon, now) in enumerate(cases):
            result = self.solar.calculate(natal, lat, lon, now)
            label = f'random {index}: natal={natal} now={now}'
            with self.subTest(case=index):
                self.assert_moment(result.start, natal, label + ' start')
                self.assert_moment(result.next, natal, label + ' next')
                self.assertTrue(as_utc(result.start) < now <= as_utc(result.next), label)
                self.assertAlmostEqual((as_utc(result.next) - as_utc(result.start)).total_seconds() / 86400, 365.2422, delta=0.02)

    def test_sr3_calculate_is_fast_enough_for_every_request(self):
        """p95 of calculate over random cases is at most 50 ms (prototype: p95 about 1 ms)."""
        durations = []
        for natal, lat, lon, now in random_cases(count=120, seed=7):
            began = time.perf_counter()
            self.solar.calculate(natal, lat, lon, now)
            durations.append(time.perf_counter() - began)
        durations.sort()
        self.assertLessEqual(durations[int(0.95 * len(durations)) - 1], 0.05)


class SolarChartTests(unittest.TestCase):
    """SR4 (red on base): the ten longitudes of the year chart at `start` against JPL, and the reference literals."""

    def setUp(self):
        self.solar = require_solar_return(self)

    def assert_points(self, result, label):
        self.assertEqual(set(result.points), set(BODIES), label)
        for symbol in BODIES:
            moment = as_utc(result.start)
            difference = abs(wrap(result.points[symbol] - tables()[symbol].at(moment)))
            self.assertLessEqual(difference, POSITION_TOLERANCE, f'{label} {symbol}: {difference * 3600:.2f} arc seconds')

    def test_sr4_reference_cases_longitudes_and_literals(self):
        for name, (asc_sign, sun_house, moon_sign, moon_house, aspects) in CHART_LITERALS.items():
            with self.subTest(case=name):
                case = CASES[name]
                result = self.solar.calculate(case['natal'], case['lat'], case['lon'], case['now'])
                self.assert_points(result, name)
                self.assertEqual((result.ascendant_sign, result.sun_house, result.moon_sign, result.moon_house),
                                 (asc_sign, sun_house, moon_sign, moon_house))
                self.assertEqual([tuple(row) for row in result.aspects], [tuple(row) for row in aspects])

    def test_sr4_random_cases_longitudes_agree_with_jpl(self):
        cases = random_cases()
        self.assertGreaterEqual(len(cases), 200)
        for index, (natal, lat, lon, now) in enumerate(cases):
            with self.subTest(case=index):
                result = self.solar.calculate(natal, lat, lon, now)
                self.assert_points(result, f'random {index}')
                self.assertEqual(result.ascendant_sign, int(result.ascendant_sign))
                self.assertTrue(0 <= result.ascendant_sign <= 11 and 0 <= result.moon_sign <= 11)
                self.assertTrue(1 <= result.sun_house <= 12 and 1 <= result.moon_house <= 12)
                self.assertTrue(len(result.aspects) <= 3)
                self.assertEqual(result.moon_sign, int(result.points['☽'] // 30))


class NatalChartAgreementTests(unittest.TestCase):
    """SR5 (red on base): a return `now` an hour after birth is the birth chart again (SC, no JPL)."""

    def setUp(self):
        self.solar = require_solar_return(self)

    def natal(self, lat, lon, birth):
        chart = Ephemeris(birth.year, birth.month, birth.day, birth.hour + birth.minute / 60, lon, lat)
        planets = chart.get_planets()
        return planets['Солнце']['абс_долгота'], planets, chart.get_house_cusps()

    def test_sr5_start_is_the_birth_moment_and_the_cusps_are_the_natal_ones(self):
        for name, (lat, lon, birth) in BIRTHS.items():
            with self.subTest(place=name):
                natal_sun, planets, natal_cusps = self.natal(lat, lon, birth)
                constructed, recorded = [], []
                real_init, real_cusps = Ephemeris.__init__, Ephemeris.get_house_cusps

                def spying_init(instance, *args, **kwargs):
                    constructed.append((args, kwargs))
                    return real_init(instance, *args, **kwargs)

                def spying_cusps(instance):
                    rows = real_cusps(instance)
                    recorded.append(rows)
                    return rows

                with patch.object(Ephemeris, '__init__', spying_init), patch.object(Ephemeris, 'get_house_cusps', spying_cusps):
                    result = self.solar.calculate(natal_sun, lat, lon, birth + timedelta(hours=1))
                self.assertLessEqual(abs((as_utc(result.start) - birth).total_seconds()), 0.5)
                self.assertEqual(len(constructed), 1, 'one year chart is one Ephemeris')
                bound = inspect.signature(real_init).bind(None, *constructed[0][0], **constructed[0][1]).arguments
                year, month, day, hour, got_lon, got_lat = (bound[key] for key in ('year', 'month', 'day', 'hour', 'lon', 'lat'))
                self.assertEqual((got_lon, got_lat), (lon, lat), 'longitude first, then latitude (as endpoints.py)')
                start = as_utc(result.start)
                expected_jd = swisseph.julday(start.year, start.month, start.day,
                                              start.hour + start.minute / 60 + start.second / 3600 + start.microsecond / 3.6e9)
                self.assertAlmostEqual(swisseph.julday(year, month, day, hour), expected_jd, delta=1 / 86400)
                self.assertGreaterEqual(len(recorded), 1, 'the house cusps come from Ephemeris.get_house_cusps')
                solar_cusps = recorded[-1]
                self.assertEqual([row['symbol'] for row in solar_cusps], [str(i) for i in range(1, 13)])
                for solar_row, natal_row in zip(solar_cusps, natal_cusps):
                    self.assertLessEqual(abs(wrap(solar_row['degree'] - natal_row['degree'])), 0.001, solar_row['symbol'])
                self.assertEqual(result.ascendant_sign, int(natal_cusps[0]['degree'] // 30))
                self.assertEqual(result.sun_house, int(planets['Солнце']['дом']))
                self.assertEqual(result.moon_house, int(planets['Луна']['дом']))
                self.assertEqual(result.moon_sign, int(planets['Луна']['абс_долгота'] // 30))
                for symbol, label in (('☉', 'Солнце'), ('☽', 'Луна'), ('♂', 'Марс'), ('♇', 'Плутон')):
                    self.assertLessEqual(abs(wrap(result.points[symbol] - planets[label]['абс_долгота'])), 0.001, symbol)


if __name__ == '__main__':
    unittest.main()
