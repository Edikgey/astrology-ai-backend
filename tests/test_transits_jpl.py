"""Independent reference for TRANSITS-1: NASA JPL Horizons tables (DECISIONS №26, DESIGN §5).

The tables in tests/fixtures/horizons/<id>.txt were downloaded once; nothing here uses the network,
Swiss Ephemeris or modules.transits. Between the daily nodes the apparent ecliptic-of-date longitude is
taken by 4-point Lagrange interpolation; transit dates are found by an hourly pass with bisection, a method
deliberately different from the module's (4-day grid, stations, binary search).

The tests in this file check the fixtures and the interpolation; they are green on the base commit.
"""
import math
import re
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

UTC = timezone.utc
FIXTURES = Path(__file__).resolve().parent / 'fixtures' / 'horizons'
BODIES = {'♃': 599, '♄': 699, '♅': 799, '♆': 899, '♇': 999}
NAMES = {599: 'Jupiter', 699: 'Saturn', 799: 'Uranus', 899: 'Neptune', 999: 'Pluto'}
ASPECTS = (('☌', 0), ('⚹', 60), ('□', 90), ('△', 120), ('☍', 180))
ORB = 1.0
EPOCH = datetime(2025, 11, 1, tzinfo=UTC)
LAST_NODE = datetime(2028, 1, 31, tzinfo=UTC)
ARCSEC = 1 / 3600
ROW = re.compile(r'^ (\d{4})-([A-Z][a-z]{2})-(\d{2}) 00:00\s+(\d+\.\d{7})\s+(-?\d+\.\d{7})\s*$')
MONTHS = {m: i + 1 for i, m in enumerate(('Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
                                          'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'))}
_TABLES = {}


def utc(*parts):
    return datetime(*parts, tzinfo=UTC)


def as_utc(moment):
    """Contract datetimes are aware UTC (DESIGN §1); a naive value is read as UTC."""
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)


def days(moment):
    return (as_utc(moment) - EPOCH).total_seconds() / 86400


def moment(day):
    return EPOCH + timedelta(seconds=round(float(day) * 86400, 3))


def raw_fixture(identifier):
    return (FIXTURES / f'{identifier}.txt').read_text(encoding='utf-8')


def table(identifier):
    """Daily longitudes from the response body ($$SOE..$$EOE), unwrapped for interpolation."""
    if identifier not in _TABLES:
        body = raw_fixture(identifier).split('$$SOE', 1)[1].split('$$EOE', 1)[0]
        dates, lons = [], []
        for line in body.splitlines():
            if not line.strip():
                continue
            match = ROW.match(line)
            if not match:
                raise AssertionError(f'unexpected Horizons row in {identifier}: {line!r}')
            dates.append(datetime(int(match[1]), MONTHS[match[2]], int(match[3]), tzinfo=UTC))
            lons.append(float(match[4]))
        _TABLES[identifier] = (dates, np.unwrap(np.array(lons), period=360.0))
    return _TABLES[identifier]


def _lagrange(identifier, t):
    dates, lon = table(identifier)
    t = np.asarray(t, dtype=float)
    i = np.floor(t).astype(int)
    if np.any(i < 1) or np.any(i > len(lon) - 3):
        raise ValueError('moment outside the interpolation range of the JPL table')
    u = t - i
    y0, y1, y2, y3 = lon[i - 1], lon[i], lon[i + 1], lon[i + 2]
    value = (-u * (u - 1) * (u - 2) / 6 * y0 + (u + 1) * (u - 1) * (u - 2) / 2 * y1
             - (u + 1) * u * (u - 2) / 2 * y2 + (u + 1) * u * (u - 1) / 6 * y3)
    return value


def lon_at(body, when):
    """Apparent geocentric ecliptic-of-date longitude (degrees, 0..360) at an aware datetime."""
    return float(_lagrange(BODIES[body], days(when))) % 360


def speed_at(body, when):
    """Degrees per day (central difference of the interpolant, ±30 min)."""
    t = days(when)
    h = 1 / 48
    return float(_lagrange(BODIES[body], t + h) - _lagrange(BODIES[body], t - h)) / (2 * h)


def wrap(value):
    """Signed difference in (-180, 180]."""
    return -((-np.asarray(value) + 180) % 360 - 180)


def separation(body, when, target):
    return float(abs(wrap(lon_at(body, when) - target)))


def targets(natal, symbol):
    """Aspect targets of one natal longitude: up to 8 (DESIGN §1)."""
    result = []
    for aspect, angle in ASPECTS:
        for value in {(natal + angle) % 360, (natal - angle) % 360}:
            result.append((aspect, value))
    return sorted(result, key=lambda item: (item[0], item[1]))


class Grid:
    """Hourly longitudes of one body over [start, end] (both ends included)."""

    def __init__(self, body, start, end):
        self.body, self.start, self.end = body, as_utc(start), as_utc(end)
        t0, t1 = days(self.start), days(self.end)
        steps = int(math.ceil((t1 - t0) * 24))
        self.t = np.append(t0 + np.arange(steps) / 24, t1)
        self.lon = _lagrange(BODIES[body], self.t)
        self.low, self.high = float(self.lon.min()), float(self.lon.max())
        self.stations = stations(body, self.start, self.end)

    def f(self, t, target):
        return wrap(_lagrange(BODIES[self.body], t) - target)

    def may_touch(self, target, margin=1.2):
        """Cheap filter: can the body come within `margin` degrees of the target at all?"""
        for shift in (-360, 0, 360):
            if self.low - margin <= target + shift <= self.high + margin:
                return True
        return False


def _bisect(fn, a, b, seconds=0.5):
    fa = fn(a)
    while (b - a) * 86400 > seconds:
        m = (a + b) / 2
        fm = fn(m)
        if (fa <= 0) == (fm <= 0):
            a, fa = m, fm
        else:
            b = m
    return (a + b) / 2


def reference_transit(grid, target, now):
    """Independent answer for one (body, target): periods intersecting [now, end] and exact moments in them.

    Returns None when no period intersects [now, end]. Periods: [start|None, end|None] as aware UTC,
    None = beyond the grid ends (DESIGN §1). Also returns diagnostics used to skip borderline cases.
    """
    if not grid.may_touch(target):
        return None
    t = grid.t
    f = wrap(grid.lon - target)
    inside = np.abs(f) <= ORB
    if not inside.any():
        return None
    now_t = days(now)
    periods, exact = [], []
    edge = np.flatnonzero(inside[1:] != inside[:-1])
    starts = [None] if inside[0] else []
    ends = []
    for k in edge:
        root = _bisect(lambda x: float(abs(grid.f(x, target))) - ORB, t[k], t[k + 1], seconds=1)
        (ends if inside[k] else starts).append(root)
    if inside[-1]:
        ends.append(None)
    for begin, finish in zip(starts, ends):
        if finish is not None and finish < now_t:
            continue
        periods.append((begin, finish))
    if not periods:
        return None
    positive = f > 0   # a node exactly on the target counts once, on its non-positive side
    crossing = np.flatnonzero((positive[1:] != positive[:-1]) & (np.abs(f[:-1]) < 90))
    for k in crossing:
        root = _bisect(lambda x: float(grid.f(x, target)), t[k], t[k + 1])
        if any((b is None or b <= root) and (e is None or root <= e) for b, e in periods):
            exact.append(root)
    # Diagnostics for borderline skips (DESIGN §5): a station closer than 30″ to the target (touch or
    # near-double root) and a station within 10″ of the orb edge make counts legitimately unstable.
    at_station = [float(abs(wrap(lon - target))) for _, _, lon in grid.stations]
    return {
        'target': target,
        'periods': [(None if b is None else moment(b), None if e is None else moment(e)) for b, e in periods],
        'exact': [moment(x) for x in exact],
        'orb_now': float(abs(grid.f(now_t, target))) if abs(grid.f(now_t, target)) <= ORB else None,
        'station_touch': any(d < 30 * ARCSEC for d in at_station),
        'near_orb_edge': any(abs(d - ORB) < 10 * ARCSEC for d in at_station),
        'boundaries': [moment(x) for p in periods for x in p if x is not None],
    }


def reference_transits(natal, start, now, end):
    """All reference transits for a natal dict {symbol: longitude} over a horizon."""
    grids = {body: Grid(body, start, end) for body in BODIES}
    rows = {}
    for point, longitude in natal.items():
        for aspect, target in targets(longitude, point):
            for body, grid in grids.items():
                found = reference_transit(grid, target, now)
                if found is not None:
                    rows[(body, aspect, point, round(target, 9))] = found
    return rows


def stations(body, start, end):
    """Moments where the interpolated speed changes sign (hourly scan + bisection)."""
    t0, t1 = days(start), days(end)
    t = np.arange(t0, t1, 1 / 24)
    h = 1 / 48
    v = (_lagrange(BODIES[body], t + h) - _lagrange(BODIES[body], t - h)) / (2 * h)
    found = []
    for k in np.flatnonzero((v[1:] > 0) != (v[:-1] > 0)):
        root = _bisect(lambda x: float(_lagrange(BODIES[body], x + h) - _lagrange(BODIES[body], x - h)), t[k], t[k + 1])
        found.append((moment(root), 'D' if v[k] < 0 else 'R', float(_lagrange(BODIES[body], root)) % 360))
    return found


class HorizonsFixtureTests(unittest.TestCase):
    """Green on base: the five tables are the unmodified one-time responses described in DESIGN §5."""

    def test_five_tables_are_kept_with_header_and_request(self):
        for body, identifier in BODIES.items():
            with self.subTest(body=body):
                text = raw_fixture(identifier)
                head, response = text.split('# ---- response ----\n', 1)
                self.assertRegex(head, r'# Requested once \(UTC[^)]*\): 2026-11-1\dT\d\d:\d\d:\d\dZ')
                self.assertIn(f"COMMAND='{identifier}'", head)
                for parameter in ("CENTER='500@399'", "START_TIME='2025-11-01'", "STOP_TIME='2028-01-31'",
                                  "STEP_SIZE='1 d'", "QUANTITIES='31'", "ANG_FORMAT='DEG'",
                                  "EXTRA_PREC='YES'", "TIME_TYPE='UT'", "EPHEM_TYPE='OBSERVER'"):
                    self.assertIn(parameter, head)
                self.assertTrue(response.startswith('API VERSION: 1.2'))
                self.assertIn('API SOURCE: NASA/JPL Horizons API', response)
                self.assertIn(f'Target body name: {NAMES[identifier]} ({identifier})', response)
                self.assertIn('Center-site name: GEOCENTRIC', response)
                self.assertIn('ecliptic-of-date longitude and latitude of the\ntarget centers\' apparent position',
                              response.replace('\r\n', '\n'))
                self.assertIn('ObsEcLon', response)

    def test_tables_are_daily_from_2025_11_01_to_2028_01_31(self):
        for body, identifier in BODIES.items():
            with self.subTest(body=body):
                dates, lon = table(identifier)
                self.assertEqual(dates[0], EPOCH)
                self.assertEqual(dates[-1], LAST_NODE)
                self.assertEqual(len(dates), (LAST_NODE - EPOCH).days + 1)
                self.assertTrue(all(b - a == timedelta(days=1) for a, b in zip(dates, dates[1:])))
                self.assertLess(float(np.max(np.abs(np.diff(lon)))), 0.3)   # slow bodies, no wrap left

    def test_interpolation_reproduces_nodes_and_is_smooth_between_them(self):
        for body, identifier in BODIES.items():
            with self.subTest(body=body):
                dates, lon = table(identifier)
                for k in (5, 300, 700):
                    self.assertAlmostEqual(lon_at(body, dates[k]), lon[k] % 360, delta=1e-9)
                    # Midpoint: 4-point Lagrange and the 6-point estimate agree far below the 2″ tolerance.
                    mid = dates[k] + timedelta(hours=12)
                    six = (3 * (lon[k - 2] + lon[k + 3]) - 25 * (lon[k - 1] + lon[k + 2])
                           + 150 * (lon[k] + lon[k + 1])) / 256
                    self.assertLess(abs(wrap(lon_at(body, mid) - six % 360)) * 3600, 0.01)

    def test_station_hints_in_design_are_confirmed_by_the_tables(self):
        """DESIGN §5 station hints, checked against JPL (date ±1 day, longitude ±0.02°)."""
        hints = {'♃': [(utc(2026, 12, 13), 'R', 147.02), (utc(2027, 4, 13), 'D', 137.00)],
                 '♄': [(utc(2026, 12, 10), 'D', 7.93), (utc(2027, 8, 9), 'R', 27.88)],
                 '♅': [(utc(2027, 2, 8), 'D', 61.68), (utc(2027, 9, 15), 'R', 69.96)],
                 '♆': [(utc(2026, 12, 12), 'D', 1.61), (utc(2027, 7, 9), 'R', 6.66)],
                 '♇': [(utc(2027, 5, 8), 'R', 307.18), (utc(2027, 10, 18), 'D', 304.75)]}
        for body, expected in hints.items():
            with self.subTest(body=body):
                found = stations(body, utc(2026, 11, 14), utc(2027, 11, 15))
                self.assertEqual([kind for _, kind, _ in found], [kind for _, kind, _ in expected])
                for (when, kind, lon), (hint_when, _, hint_lon) in zip(found, expected):
                    self.assertLessEqual(abs((when - hint_when).total_seconds()), 1.5 * 86400)
                    self.assertLess(abs(wrap(lon - hint_lon)), 0.02)


if __name__ == '__main__':
    unittest.main()
