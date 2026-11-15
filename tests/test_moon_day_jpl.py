"""LUNAR-1 accuracy regression LD3-LD6 against NASA JPL Horizons (DECISIONS 26, 30; tasks/LUNAR-1/DESIGN.md sections 2, 5, 7).

`modules.moon_day` is called through the DESIGN section 2 contract only: calculate(natal, zone, now) -> MoonDay with
date, zone, phase_code, phase_exact, illumination, sign_index, ingress, aspects.
The tables in tests/fixtures/horizons/301.txt (Moon: Illu%, ecliptic longitude) and 10.txt (Sun: longitude) were
downloaded once (step 6 h, 2026-11-01 .. 2028-01-01). Nothing here uses the network, Swiss Ephemeris or modules.*:
the reference reads the tables by 4-point Lagrange interpolation and finds every event by a relative sweep with
bisection, a method deliberately different from the module's.

The fixture and reference tests (HorizonsFixtureTests, ReferenceLiteralTests) are green on the base commit; the tests
that call modules.moon_day are red there because the module does not exist yet.
"""
import hashlib
import importlib
import math
import random
import re
import time
import unittest
from datetime import date, datetime, time as day_time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

UTC = timezone.utc
FIXTURES = Path(__file__).resolve().parent / 'fixtures' / 'horizons'
EPOCH = datetime(2026, 11, 1, tzinfo=UTC)
STEP = 6 * 3600                          # seconds between table nodes
ROWS = 1705                              # 2026-11-01 00:00 .. 2028-01-01 00:00 every 6 h, both ends included
ARCSEC = 1 / 3600
TOLERANCE = 3 * ARCSEC                   # |lambda_JPL(t_module) - target| (DESIGN section 5)
ILLUMINATION_TOLERANCE = 0.01            # percentage points
BOUNDARY = 5                             # seconds: events this close to a day or minute boundary are not compared
SHA256 = {301: '006c53dd8f995fe77c991dad591cb2edfd2d2851efb8ff8dd64568ed241da90d',
          10: '117ac70de6cd97867435a539b36607d02a452ec5436cb0a7b071458caf07415f'}
MONTHS = {m: i + 1 for i, m in enumerate(('Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
                                          'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'))}
MOON_ROW = re.compile(r'^ (\d{4})-([A-Z][a-z]{2})-(\d{2}) (\d{2}):(\d{2})\s+(\d+\.\d+)\s+(\d+\.\d+)\s+(-?\d+\.\d+)$')
SUN_ROW = re.compile(r'^ (\d{4})-([A-Z][a-z]{2})-(\d{2}) (\d{2}):(\d{2})\s+(\d+\.\d+)\s+(-?\d+\.\d+)$')
ASPECTS = (('☌', 0), ('⚹', 60), ('□', 90), ('△', 120), ('☍', 180))
ANGLE = dict(ASPECTS)
POINT_ORDER = ('☉', '☽', '☿', '♀', '♂', '♃', '♄', '♅', '♆', '♇', 'AS', 'MC')
QUARTERS = (('new_moon', 0), ('first_quarter', 90), ('full_moon', 180), ('last_quarter', 270))
HALVES = ('waxing_crescent', 'waxing_gibbous', 'waning_gibbous', 'waning_crescent')
ZONES = ('Europe/Kyiv', 'America/New_York', 'Australia/Sydney', 'Asia/Kathmandu',
         'America/Sao_Paulo', 'Pacific/Kiritimati', 'Pacific/Pago_Pago', 'UTC')


def utc(*parts):
    return datetime(*parts, tzinfo=UTC)


def as_utc(moment):
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)


def seconds(moment):
    return (as_utc(moment) - EPOCH).total_seconds()


def moment_of(sec):
    return EPOCH + timedelta(seconds=sec)


def wrap(value):
    """Signed difference in (-180, 180]."""
    return -((-value + 180) % 360 - 180)


def raw_bytes(identifier):
    return (FIXTURES / f'{identifier}.txt').read_bytes()


def raw_text(identifier):
    return (FIXTURES / f'{identifier}.txt').read_text(encoding='utf-8')     # universal newlines: CRLF or LF


_TABLES = {}


def table(identifier):
    """(Illu% or None, unwrapped longitude) lists of one fixture, one value per 6-hour node."""
    if identifier not in _TABLES:
        body = raw_text(identifier).split('$$SOE', 1)[1].split('$$EOE', 1)[0]
        stamps, illu, lon = [], [], []
        for line in body.splitlines():
            line = line.rstrip()
            if not line.strip():
                continue
            match = (MOON_ROW if identifier == 301 else SUN_ROW).match(line)
            if not match:
                raise AssertionError(f'unexpected Horizons row in {identifier}: {line!r}')
            stamps.append(datetime(int(match[1]), MONTHS[match[2]], int(match[3]), int(match[4]), int(match[5]), tzinfo=UTC))
            if identifier == 301:
                illu.append(float(match[6]))
                lon.append(float(match[7]))
            else:
                lon.append(float(match[6]))
        _TABLES[identifier] = (stamps, illu, np.unwrap(np.array(lon), period=360.0).tolist())
    return _TABLES[identifier]


def _lagrange(values, t):
    i = math.floor(t)
    if i < 1 or i > len(values) - 3:
        raise ValueError('moment outside the interpolation range of the JPL table')
    u = t - i
    y0, y1, y2, y3 = values[i - 1:i + 3]
    return (-u * (u - 1) * (u - 2) / 6 * y0 + (u + 1) * (u - 1) * (u - 2) / 2 * y1
            - (u + 1) * u * (u - 2) / 2 * y2 + (u + 1) * u * (u - 1) / 6 * y3)


def moon_s(sec):
    return _lagrange(table(301)[2], sec / STEP) % 360


def sun_s(sec):
    return _lagrange(table(10)[2], sec / STEP) % 360


def elongation_s(sec):
    return (_lagrange(table(301)[2], sec / STEP) - _lagrange(table(10)[2], sec / STEP)) % 360


def illumination_s(sec):
    return _lagrange(table(301)[1], sec / STEP)


def moon_lon(moment):
    return moon_s(seconds(moment))


def elongation(moment):
    return elongation_s(seconds(moment))


def illumination(moment):
    return illumination_s(seconds(moment))


def crossings(fn, start, end, targets):
    """Moments in [start, end) where the growing angle fn passes a target: [(moment, key)] sorted by moment.

    The angle only grows, so the sweep fn(t) - fn(start) (mod 360) is monotonic: a target is inside the day if its
    distance ahead of the start is smaller than the whole sweep; then it is found by bisection to 0.05 s.
    """
    lo0, hi0 = seconds(start), seconds(end)
    base = fn(lo0)
    total = (fn(hi0) - base) % 360
    found = []
    for key, target in targets:
        ahead = (target - base) % 360
        if ahead >= total:
            continue
        lo, hi = lo0, hi0
        while hi - lo > 0.05:
            mid = (lo + hi) / 2
            if (fn(mid) - base) % 360 < ahead:
                lo = mid
            else:
                hi = mid
        found.append((moment_of((lo + hi) / 2), key))
    return sorted(found, key=lambda item: item[0])


def day_bounds(zone, day):
    start = datetime.combine(day, day_time.min, zone).astimezone(UTC)
    end = datetime.combine(day + timedelta(days=1), day_time.min, zone).astimezone(UTC)
    return start, end


def local_noon(zone, day):
    return datetime.combine(day, day_time(12, 0), zone).astimezone(UTC)


def hhmm(moment, zone):
    return as_utc(moment).astimezone(zone).strftime('%H:%M')


def minute_safe(moment, zone):
    """True when the reference moment is at least BOUNDARY seconds from a minute boundary (HH:MM is then unambiguous)."""
    local = as_utc(moment).astimezone(zone)
    sec = local.second + local.microsecond / 1e6
    return min(sec, 60 - sec) >= BOUNDARY


def second_of_day(moment, zone):
    local = as_utc(moment).astimezone(zone)
    return local.hour * 3600 + local.minute * 60 + local.second + local.microsecond / 1e6


def literal_seconds(text):
    hours, minutes, secs = (int(part) for part in text.split(':'))
    return hours * 3600 + minutes * 60 + secs


def aspect_targets(natal):
    result = []
    for point, longitude in natal.items():
        for symbol, angle in ASPECTS:
            for target in aspect_target_values(longitude, angle):
                result.append(((symbol, point), target))
    return result


def aspect_target_values(longitude, angle):
    """Longitudes where the Moon makes the aspect: natal +- angle; conjunction and opposition have one target."""
    if angle in (0, 180):
        return [(longitude + angle) % 360]
    return sorted({(longitude + angle) % 360, (longitude - angle) % 360})


def aspect_target_of(natal, symbol, point):
    """Both targets natal +- angle; the caller picks the one nearest to the module's moment."""
    return aspect_target_values(natal[point], ANGLE[symbol])


def reference_day(zone_name, day, natal):
    """Independent answer for one local day (DESIGN section 2) read from the JPL tables only."""
    zone = ZoneInfo(zone_name)
    start, end = day_bounds(zone, day)
    noon = local_noon(zone, day)
    phase_events = crossings(elongation_s, start, end, QUARTERS_AS_TARGETS)
    elong_noon = elongation(noon)
    sign_start = int((moon_lon(start) // 30) % 12)
    ingress = crossings(moon_s, start, end, [(k, 30 * k) for k in range(12)])
    aspects = crossings(moon_s, start, end, aspect_targets(natal))
    order = {point: index for index, point in enumerate(POINT_ORDER)}
    aspects.sort(key=lambda item: (round(seconds(item[0]), 1), order[item[1][1]], ANGLE[item[1][0]]))
    return {
        'zone': zone, 'start': start, 'end': end, 'noon': noon,
        'phase_event': phase_events[0] if phase_events else None,
        'phase_half': HALVES[int(elong_noon // 90) % 4],
        'illumination': illumination(noon),
        'sign_start': sign_start,
        'ingress': ingress[0] if ingress else None,
        'aspects': [(when, symbol, point) for when, (symbol, point) in aspects],
    }


QUARTERS_AS_TARGETS = [(name, angle) for name, angle in QUARTERS]


def reference_phase_code(info):
    return info['phase_event'][1] if info['phase_event'] else info['phase_half']


# --- DESIGN section 5: reference days -------------------------------------------------------------------------------
# id: (zone, date, phase code, exact local time or None, whole percent, Illu% at local noon, sign at start,
#      ingress (local time, new sign) or None). The times are the DESIGN literals (moment found independently).
DAYS = {
    'A': ('Europe/Kyiv', date(2026, 11, 15), 'waxing_crescent', None, 31, 30.79, 9, ('11:24:14', 10)),
    'B': ('Europe/Kyiv', date(2026, 11, 24), 'full_moon', '16:53:33', 100, 99.75, 1, ('13:09:37', 2)),
    'Q3': ('Europe/Kyiv', date(2026, 12, 1), 'last_quarter', '08:08:39', 48, 48.38, 5, None),
    'N1': ('America/New_York', date(2026, 12, 8), 'new_moon', '19:51:51', 0, 0.27, 8, None),
    'N2': ('Europe/Kyiv', date(2026, 12, 9), 'new_moon', '02:51:51', 0, 0.28, 8, None),
    'N0': ('Europe/Kyiv', date(2026, 12, 8), 'waning_crescent', None, 1, 0.53, 8, None),
    'Q1': ('Europe/Kyiv', date(2026, 12, 17), 'first_quarter', '07:42:40', 52, 51.95, 11, ('16:34:21', 0)),
    'D25': ('America/New_York', date(2027, 11, 7), 'waxing_gibbous', None, 63, 62.95, 10, ('12:09:00', 11)),
    'D23': ('America/New_York', date(2027, 3, 14), 'waxing_crescent', None, 39, 38.74, 2, None),
    'K': ('Asia/Kathmandu', date(2027, 2, 17), 'waxing_gibbous', None, 81, 81.28, 2, ('00:23:25', 3)),
    'S': ('America/Sao_Paulo', date(2027, 2, 9), 'waxing_crescent', None, 8, 8.26, 11, ('23:48:36', 0)),
}
# D25: the JPL ingress moment 12:09:00 sits on a minute boundary: literal_seconds % 60 is within 5 s, so only the residual is checked.

# Natal points are lambda_JPL at the chosen moment +- angle, so the aspect is exact there by construction.
NATAL_A = {'♀': 176.004891, 'MC': 29.156217, '♄': 122.310525, '☉': 246.216503, '♂': 294.382484}
NATAL_Q1 = {'♃': 0.945051, 'AS': 180.945051}
NATAL_D23 = {'♇': 77.318151, '♅': 77.900257}
NATAL_D25 = {'♆': 335.630494}
# (natal, [(local time, aspect, point)]) in the order of DESIGN section 2: moment, point order, angle.
ASPECT_SETS = {
    'A': (NATAL_A, [('03:17:25', '△', '♀'), ('09:41:29', '□', 'MC'), ('16:05:20', '☍', '♄'), ('23:59:29', '⚹', '☉')]),
    'Q1': (NATAL_Q1, [('18:20:15', '☌', '♃'), ('18:20:15', '☍', 'AS')]),
    'D23': (NATAL_D23, [('23:30:10', '☌', '♇')]),
    'D25': (NATAL_D25, [('23:30:10', '☌', '♆')]),
}


def noon_now(day_id):
    zone_name, day = DAYS[day_id][:2]
    return local_noon(ZoneInfo(zone_name), day)


def require_moon_day(test):
    try:
        return importlib.import_module('modules.moon_day')
    except ModuleNotFoundError as error:
        if error.name != 'modules.moon_day':
            raise
        test.fail('modules.moon_day is missing: the DESIGN section 2 contract is not implemented yet')


class HorizonsFixtureTests(unittest.TestCase):
    """Green on base: the two tables are the unmodified one-time responses described in DESIGN section 5."""

    def test_fixtures_are_the_unmodified_downloads(self):
        for identifier, digest in SHA256.items():
            with self.subTest(identifier=identifier):
                lf_form = raw_bytes(identifier).replace(b'\r\n', b'\n')   # core.autocrlf=true: CRLF on disk
                self.assertEqual(hashlib.sha256(lf_form).hexdigest(), digest)

    def test_headers_and_requests(self):
        for identifier, quantities, name in ((301, '10,31', 'Moon'), (10, '31', 'Sun')):
            with self.subTest(identifier=identifier):
                head, response = raw_text(identifier).split('# ---- response ----\n', 1)
                self.assertRegex(head, r'# Requested once \(UTC[^)]*\): 2026-11-15T08:0\d:\d\dZ')
                for parameter in (f"COMMAND='{identifier}'", "CENTER='500@399'", "START_TIME='2026-11-01'",
                                  "STOP_TIME='2028-01-01'", "STEP_SIZE='6 h'", f"QUANTITIES='{quantities}'",
                                  "ANG_FORMAT='DEG'", "EXTRA_PREC='YES'", "TIME_TYPE='UT'", "EPHEM_TYPE='OBSERVER'"):
                    self.assertIn(parameter, head)
                self.assertTrue(response.startswith('API VERSION: 1.2'))
                self.assertIn('API SOURCE: NASA/JPL Horizons API', response)
                self.assertIn(f'Target body name: {name} ({identifier})', response)
                self.assertIn('Center-site name: GEOCENTRIC', response)
                self.assertIn('ObsEcLon', response)
        self.assertIn('Illu%', raw_text(301))

    def test_tables_are_six_hourly_and_complete(self):
        for identifier in (301, 10):
            with self.subTest(identifier=identifier):
                stamps, illu, lon = table(identifier)
                self.assertEqual(len(stamps), ROWS)
                self.assertEqual(stamps[0], EPOCH)
                self.assertEqual(stamps[-1], utc(2028, 1, 1))
                self.assertTrue(all(b - a == timedelta(hours=6) for a, b in zip(stamps, stamps[1:])))
                self.assertEqual(len(lon), ROWS)
                self.assertLess(max(abs(b - a) for a, b in zip(lon, lon[1:])), 4.5)   # Moon <= 4 deg per 6 h, no wrap left
                if identifier == 301:
                    self.assertEqual(len(illu), ROWS)
                    self.assertTrue(all(0 <= value <= 100 for value in illu))

    def test_interpolation_reproduces_nodes_and_is_smooth_between_them(self):
        stamps, illu, lon = table(301)
        for k in (5, 300, 900, 1500):
            self.assertAlmostEqual(moon_lon(stamps[k]), lon[k] % 360, delta=1e-9)
            self.assertAlmostEqual(illumination(stamps[k]), illu[k], delta=1e-9)
            mid = stamps[k] + timedelta(hours=3)
            six = (3 * (lon[k - 2] + lon[k + 3]) - 25 * (lon[k - 1] + lon[k + 2]) + 150 * (lon[k] + lon[k + 1])) / 256
            # 6-point estimate at the midpoint of two nodes: the 4-point value agrees with it far below 3 arc seconds.
            self.assertLess(abs(wrap(moon_lon(mid) - six % 360)) * 3600, 0.5)


class ReferenceLiteralTests(unittest.TestCase):
    """Green on base: the DESIGN literals are re-derived from the tables by the independent reference."""

    def test_days_match_the_design_table(self):
        for day_id, (zone_name, day, code, exact, whole, noon_value, sign, ingress) in DAYS.items():
            with self.subTest(day=day_id):
                info = reference_day(zone_name, day, NATAL_A)
                zone = info['zone']
                self.assertEqual(reference_phase_code(info), code)
                if exact is None:
                    self.assertIsNone(info['phase_event'])
                else:
                    self.assertAlmostEqual(second_of_day(info['phase_event'][0], zone), literal_seconds(exact), delta=1.5)
                self.assertAlmostEqual(info['illumination'], noon_value, delta=0.006)
                self.assertEqual(round(info['illumination']), whole)
                self.assertEqual(info['sign_start'], sign)
                if ingress is None:
                    self.assertIsNone(info['ingress'])
                else:
                    self.assertAlmostEqual(second_of_day(info['ingress'][0], zone), literal_seconds(ingress[0]), delta=1.5)
                    self.assertEqual(info['ingress'][1], ingress[1])

    def test_aspect_sets_match_the_design_table(self):
        for day_id, (natal, expected) in ASPECT_SETS.items():
            with self.subTest(day=day_id):
                zone_name, day = DAYS[day_id][:2]
                info = reference_day(zone_name, day, natal)
                rows = [(round(second_of_day(when, info['zone'])), symbol, point) for when, symbol, point in info['aspects']]
                self.assertEqual([(symbol, point) for _, symbol, point in rows], [(s, p) for _, s, p in expected])
                for (found, _, _), (literal, _, _) in zip(rows, expected):
                    self.assertAlmostEqual(found, literal_seconds(literal), delta=1.5)

    def test_boundary_cases_are_what_the_design_says(self):
        """A: Mars conjunction is 30 s before the day starts; D23: Uranus conjunction is 30 min after the day ends."""
        info = reference_day('Europe/Kyiv', date(2026, 11, 15), NATAL_A)
        self.assertNotIn('♂', [point for _, _, point in info['aspects']])
        before = crossings(moon_s, info['start'] - timedelta(hours=1), info['start'], [('x', NATAL_A['♂'])])
        self.assertAlmostEqual((info['start'] - before[0][0]).total_seconds(), 30, delta=3)
        info = reference_day('America/New_York', date(2027, 3, 14), NATAL_D23)
        self.assertEqual((info['end'] - info['start']), timedelta(hours=23))
        after = crossings(moon_s, info['end'], info['end'] + timedelta(hours=2), [('x', NATAL_D23['♅'])])
        self.assertAlmostEqual((after[0][0] - info['end']).total_seconds(), 1810, delta=5)
        info = reference_day('America/New_York', date(2027, 11, 7), NATAL_D25)
        self.assertEqual((info['end'] - info['start']), timedelta(hours=25))


class MoonDayJplTests(unittest.TestCase):
    def setUp(self):
        self.moon_day = require_moon_day(self)

    def calc(self, natal, zone_name, now):
        return self.moon_day.calculate(natal, ZoneInfo(zone_name), now)

    def assert_event_matches(self, moment, zone, target, fn, literal=None):
        """Residual <= 3 arc seconds at the module's moment; local HH:MM equals the literal unless that is within 5 s of a minute."""
        self.assertIsInstance(moment, datetime)
        self.assertIsNotNone(moment.tzinfo, 'event moments are aware UTC datetimes (DESIGN section 2)')
        self.assertLessEqual(abs(wrap(fn(moment) - target)), TOLERANCE, f'{moment} vs {target}')
        if literal is not None and min(literal_seconds(literal) % 60, 60 - literal_seconds(literal) % 60) >= BOUNDARY:
            self.assertEqual(hhmm(moment, zone), literal[:5])

    def test_ld3_phase_code_exact_moment_and_illumination(self):
        for day_id, (zone_name, day, code, exact, whole, noon_value, sign, ingress) in DAYS.items():
            with self.subTest(day=day_id):
                zone = ZoneInfo(zone_name)
                result = self.calc(NATAL_A, zone_name, noon_now(day_id))
                self.assertEqual(result.date, day)
                self.assertEqual(str(result.zone), zone_name)
                self.assertEqual(result.phase_code, code)
                if exact is None:
                    self.assertIsNone(result.phase_exact)
                else:
                    target = dict(QUARTERS)[code]
                    self.assert_event_matches(result.phase_exact, zone, target, elongation, exact)
                self.assertLessEqual(abs(100 * result.illumination - illumination(noon_now(day_id))), ILLUMINATION_TOLERANCE)
                self.assertAlmostEqual(100 * result.illumination, noon_value, delta=ILLUMINATION_TOLERANCE + 0.006)
                self.assertEqual(round(100 * result.illumination), whole)

    def test_ld3_quarter_moment_is_the_same_instant_for_two_local_dates(self):
        """The new moon of 2026-12-09 02:51 Kyiv is 2026-12-08 19:51 in New York: one instant, two local days."""
        first = self.calc(NATAL_A, 'America/New_York', noon_now('N1'))
        second = self.calc(NATAL_A, 'Europe/Kyiv', noon_now('N2'))
        self.assertEqual((first.phase_code, second.phase_code), ('new_moon', 'new_moon'))
        self.assertLessEqual(abs((as_utc(first.phase_exact) - as_utc(second.phase_exact)).total_seconds()), 1)
        self.assertEqual((first.date, second.date), (date(2026, 12, 8), date(2026, 12, 9)))

    def test_ld4_sign_at_start_of_day_and_ingress(self):
        for day_id, (zone_name, day, code, exact, whole, noon_value, sign, ingress) in DAYS.items():
            with self.subTest(day=day_id):
                zone = ZoneInfo(zone_name)
                result = self.calc(NATAL_A, zone_name, noon_now(day_id))
                self.assertEqual(result.sign_index, sign)
                if ingress is None:
                    self.assertIsNone(result.ingress)
                    continue
                moment, index = result.ingress
                self.assertEqual(index, ingress[1])
                self.assert_event_matches(moment, zone, 30 * ingress[1], moon_lon, ingress[0])

    def test_ld4_ingress_through_zero_degrees_is_an_ingress_into_aries(self):
        for day_id in ('Q1', 'S'):
            with self.subTest(day=day_id):
                result = self.calc(NATAL_A, DAYS[day_id][0], noon_now(day_id))
                self.assertEqual(result.sign_index, 11)
                self.assertEqual(result.ingress[1], 0)

    def test_ld5_aspects_order_residual_and_day_boundaries(self):
        for day_id, (natal, expected) in ASPECT_SETS.items():
            with self.subTest(day=day_id):
                zone_name = DAYS[day_id][0]
                zone = ZoneInfo(zone_name)
                result = self.calc(natal, zone_name, noon_now(day_id))
                self.assertEqual([(symbol, point) for _, symbol, point in result.aspects],
                                 [(symbol, point) for _, symbol, point in expected])
                for (moment, symbol, point), (literal, _, _) in zip(result.aspects, expected):
                    targets = aspect_target_of(natal, symbol, point)
                    best = min(targets, key=lambda target: abs(wrap(moon_lon(moment) - target)))
                    self.assert_event_matches(moment, zone, best, moon_lon, literal)

    def test_ld5_same_instant_keeps_point_order(self):
        """Q1: Jupiter conjunction and Ascendant opposition are one moment; Jupiter comes first (DESIGN section 2)."""
        result = self.calc(NATAL_Q1, *self._q1_args())
        (first, _, point_a), (second, _, point_b) = result.aspects
        self.assertEqual((point_a, point_b), ('♃', 'AS'))
        self.assertLessEqual(abs((as_utc(first) - as_utc(second)).total_seconds()), 1)

    def _q1_args(self):
        return DAYS['Q1'][0], noon_now('Q1')

    def test_ld5_day_boundaries_are_half_open_for_a_target_30_seconds_outside(self):
        """A: Mars conjunction 30 s before 00:00 and D23: Uranus conjunction after the day end are not in the answer."""
        a = self.calc(NATAL_A, *self._args('A'))
        self.assertNotIn('♂', [point for _, _, point in a.aspects])
        d = self.calc(NATAL_D23, *self._args('D23'))
        self.assertEqual([point for _, _, point in d.aspects], ['♇'])
        self.assertEqual(len(self.calc(NATAL_D25, *self._args('D25')).aspects), 1)      # 25th hour is still inside

    def _args(self, day_id):
        return DAYS[day_id][0], noon_now(day_id)

    def test_ld5_the_answer_does_not_depend_on_the_time_of_day_of_now(self):
        for day_id in ('A', 'D25'):
            with self.subTest(day=day_id):
                zone_name, day = DAYS[day_id][:2]
                start, end = day_bounds(ZoneInfo(zone_name), day)
                natal = ASPECT_SETS[day_id][0]
                baseline = self.calc(natal, zone_name, noon_now(day_id))
                for now in (start, start + timedelta(seconds=1), end - timedelta(seconds=1)):
                    result = self.calc(natal, zone_name, now)
                    self.assertEqual((result.date, result.phase_code, result.sign_index),
                                     (baseline.date, baseline.phase_code, baseline.sign_index))
                    self.assertEqual(result.aspects, baseline.aspects)
                    self.assertEqual(result.ingress, baseline.ingress)
                    self.assertEqual(result.illumination, baseline.illumination)
                after = self.calc(natal, zone_name, end)      # the next local day starts exactly at `end`
                self.assertEqual(after.date, day + timedelta(days=1))


def random_cases(count=208, seed=20261115):
    """(zone name, local date, natal, now): count days spread over eight zones, random natal points, random `now`."""
    rng = random.Random(seed)
    first, last = date(2026, 11, 20), date(2027, 12, 20)
    span = (last - first).days
    cases = []
    for index in range(count):
        zone_name = ZONES[index % len(ZONES)]
        day = first + timedelta(days=rng.randrange(span + 1))
        natal = {point: round(rng.uniform(0, 360), 6) for point in POINT_ORDER}
        start, end = day_bounds(ZoneInfo(zone_name), day)
        now = start + (end - start) * rng.uniform(0, 0.999)
        cases.append((zone_name, day, natal, now))
    return cases


class RandomWalkTests(unittest.TestCase):
    """LD6: the module against the independent reference over >= 200 random days, 8 zones, random natal points."""

    def setUp(self):
        self.moon_day = require_moon_day(self)

    def near_day_edge(self, moment, start, end):
        return min(abs((as_utc(moment) - start).total_seconds()), abs((as_utc(moment) - end).total_seconds())) < BOUNDARY

    def test_ld6_random_days_agree_with_the_independent_pass(self):
        cases = random_cases()
        self.assertGreaterEqual(len(cases), 200)
        compared = {'phase': 0, 'sign': 0, 'ingress': 0, 'aspects': 0}
        for zone_name, day, natal, now in cases:
            label = f'{zone_name} {day}'
            zone = ZoneInfo(zone_name)
            info = reference_day(zone_name, day, natal)
            start, end = info['start'], info['end']
            result = self.moon_day.calculate(natal, zone, now)
            with self.subTest(case=label):
                self.assertEqual(result.date, day)
                self.assertLessEqual(abs(100 * result.illumination - info['illumination']), ILLUMINATION_TOLERANCE)
                event = info['phase_event']
                phase_edge = event is not None and self.near_day_edge(event[0], start, end)
                part = elongation(info['noon']) % 90
                elong_edge = min(part, 90 - part) < 5 * ARCSEC
                if not phase_edge and not elong_edge:
                    self.assertEqual(result.phase_code, reference_phase_code(info), label)
                    compared['phase'] += 1
                    if event is None:
                        self.assertIsNone(result.phase_exact, label)
                    else:
                        self.assert_moment(result.phase_exact, zone, dict(QUARTERS)[event[1]], elongation, event[0])
                lam = moon_lon(start) % 30
                if min(lam, 30 - lam) >= 5 * ARCSEC:
                    self.assertEqual(result.sign_index, info['sign_start'], label)
                    compared['sign'] += 1
                ingress = info['ingress']
                module_ingress = result.ingress
                if ingress is not None and self.near_day_edge(ingress[0], start, end):
                    pass
                elif module_ingress is not None and self.near_day_edge(module_ingress[0], start, end):
                    pass
                else:
                    compared['ingress'] += 1
                    if ingress is None:
                        self.assertIsNone(module_ingress, label)
                    else:
                        self.assertIsNotNone(module_ingress, label)
                        self.assertEqual(module_ingress[1], ingress[1], label)
                        self.assert_moment(module_ingress[0], zone, 30 * ingress[1], moon_lon, ingress[0])
                reference = [row for row in info['aspects'] if not self.near_day_edge(row[0], start, end)]
                module = [row for row in result.aspects if not self.near_day_edge(row[0], start, end)]
                self.assertEqual({(s, p) for _, s, p in module}, {(s, p) for _, s, p in reference}, label)
                self.assertEqual(len(module), len(reference), label)
                moments = [as_utc(row[0]) for row in module]
                self.assertEqual(moments, sorted(moments), f'aspects are ordered by moment: {label}')
                by_key = {(s, p): when for when, s, p in reference}
                for moment, symbol, point in module:
                    best = min(aspect_target_of(natal, symbol, point), key=lambda t: abs(wrap(moon_lon(moment) - t)))
                    self.assert_moment(moment, zone, best, moon_lon, by_key[(symbol, point)])
                    compared['aspects'] += 1
        self.assertGreater(compared['phase'], 190)
        self.assertGreater(compared['sign'], 190)
        self.assertGreater(compared['aspects'], 400)
        self.assertGreater(compared['ingress'], 190)

    def assert_moment(self, moment, zone, target, fn, reference_moment):
        self.assertIsNotNone(moment.tzinfo)
        self.assertLessEqual(abs(wrap(fn(moment) - target)), TOLERANCE, f'{moment} vs {target}')
        if minute_safe(reference_moment, zone):
            self.assertEqual(hhmm(moment, zone), hhmm(reference_moment, zone))

    def test_ld6_calculate_is_fast_enough_for_every_request(self):
        """p95 of calculate over the random days is at most 50 ms (prototype: about 7 ms)."""
        durations = []
        for zone_name, day, natal, now in random_cases(count=120, seed=7):
            began = time.perf_counter()
            self.moon_day.calculate(natal, ZoneInfo(zone_name), now)
            durations.append(time.perf_counter() - began)
        durations.sort()
        self.assertLessEqual(durations[int(0.95 * len(durations)) - 1], 0.05)


if __name__ == '__main__':
    unittest.main()
