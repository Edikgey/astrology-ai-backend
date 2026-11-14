"""TRANSITS-1 accuracy regression TA1-TA15 against NASA JPL Horizons (DESIGN §5, DECISIONS №26).

`modules.transits` is called through the DESIGN §1 contract only: horizon, sky_now, find_transits.
Expected local dates below are literals from an independent hourly pass over the interpolated JPL tables
(test_transits_jpl.reference_transit); ReferenceLiteralTests re-derives them from the tables (green on base).
Bodies, aspects and points are the symbols of DESIGN §1 (natal dict[symbol], ♃♄♅♆♇, synastry.MAJOR_ASPECTS).
"""
import importlib
import random
import time
import unittest
import unittest.mock
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import test_transits_jpl as J
from test_transits_jpl import ARCSEC, utc

NOW = utc(2026, 11, 14, 2, 15)
ANGLES = dict(J.ASPECTS)
SLOW = ('♃', '♄', '♅', '♆', '♇')
POINTS = ('☉', '☽', '☿', '♀', '♂', '♃', '♄', '♅', '♆', '♇', 'AS', 'MC')
EXACT_TOLERANCE = 3 * ARCSEC          # |λ_JPL(t_module) − target| (2″ ephemeris + 1 min search)
POSITION_TOLERANCE = 2 * ARCSEC


def require_transits(test):
    try:
        return importlib.import_module('modules.transits')
    except ModuleNotFoundError as error:
        if error.name != 'modules.transits':
            raise
        test.fail('modules.transits is missing: the DESIGN §1 contract is not implemented yet')


def iso(moment, zone):
    return None if moment is None else J.as_utc(moment).astimezone(zone).date().isoformat()


def near_midnight(moment, zone, hours=2):
    local = J.as_utc(moment).astimezone(zone)
    since = local - local.replace(hour=0, minute=0, second=0, microsecond=0)
    return min(since, timedelta(days=1) - since) < timedelta(hours=hours)


def reference_horizon(zone, now=NOW):
    zone = ZoneInfo(zone) if isinstance(zone, str) else zone
    today = now.astimezone(zone).date()
    last = date(today.year + 1, 2, 28) if (today.month, today.day) == (2, 29) else today.replace(year=today.year + 1)
    end = datetime.combine(last + timedelta(days=1), datetime.min.time(), zone).astimezone(J.UTC)
    return now - timedelta(days=366), end


# Each case: natal dict, the transit under test and the expected answer as local dates in `zone`.
# `t0` cases are constructed so the exact moment is t0 by definition (natal = λ_JPL(t0) ∓ angle).
CASES = {
    'TA2': dict(body='♅', aspect='□', point='☉', t0=utc(2027, 6, 20, 12, 0), zone='Europe/Kyiv',
                periods=[('2027-06-03', '2027-07-09')], exact=['2027-06-20'], active=False),
    'TA3': dict(body='♄', aspect='☌', point='☉', t0=utc(2027, 3, 10, 3, 30), zone='America/New_York',
                periods=[('2027-03-01', '2027-03-18')], exact=['2027-03-09'], active=False),
    'TA4': dict(body='♄', aspect='☍', point='☉', t0=utc(2027, 3, 15, 4, 30), zone='America/New_York',
                periods=[('2027-03-06', '2027-03-23')], exact=['2027-03-15'], active=False),
    'TA5': dict(body='♃', aspect='△', point='☉', t0=utc(2027, 10, 3, 13, 30), zone='Australia/Sydney',
                periods=[('2027-09-29', '2027-10-08')], exact=['2027-10-04'], active=False),
    'TA6': dict(body='♆', aspect='⚹', point='☉', t0=utc(2027, 3, 25, 1, 0), zone='America/Sao_Paulo',
                periods=[('2027-02-25', '2027-04-21'), ('2027-10-03', None)], exact=['2027-03-24'], active=False),
    'TA7': dict(body='♇', aspect='⚹', point='☉', t0=utc(2027, 2, 9, 19, 45), zone='Asia/Kathmandu',
                periods=[('2027-01-09', '2027-03-18'), ('2027-06-30', None)], exact=['2027-02-10', '2027-08-14'],
                active=False),
    'TA8': dict(body='♃', aspect='☌', point='☉', natal={'☉': 146.5}, zone='Europe/Kyiv',
                periods=[('2026-11-11', '2027-01-13'), ('2027-07-03', '2027-07-13')],
                exact=['2026-11-25', '2026-12-31', '2027-07-08'], active=True),
    'TA9': dict(body='♇', aspect='☌', point='☉', natal={'☉': 304.77}, zone='UTC',
                periods=[('2026-12-10', '2027-02-15'), ('2027-08-06', None)],
                exact=['2027-01-14', '2027-10-08', '2027-10-27'], active=False),
    'TA10': dict(body='♃', aspect='☌', point='☉', natal={'☉': 147.52}, zone='Europe/Kyiv',
                 periods=[('2026-11-25', '2026-12-30'), ('2027-07-08', '2027-07-18')],
                 exact=['2027-07-13'], active=False),
    'TA11': dict(body='♃', aspect='☌', point='☉', natal_at=NOW - timedelta(days=5), zone='Europe/Kyiv',
                 periods=[('2026-10-31', '2026-11-20'), ('2027-01-04', '2027-01-25'), ('2027-06-26', '2027-07-07')],
                 exact=['2026-11-09', '2027-01-15', '2027-07-01'], active=True),
    'TA12a': dict(body='♄', aspect='☌', point='☉', natal_at=utc(2027, 11, 14, 20), zone='Europe/Kyiv',
                  periods=[('2027-05-07', '2027-05-25'), ('2027-10-31', None)],
                  exact=['2027-05-16', '2027-11-14'], active=False),
    'TA12b': dict(body='♄', aspect='☌', point='☉', natal_at=utc(2027, 11, 15, 20), zone='Europe/Kyiv',
                  periods=[('2027-05-07', '2027-05-24'), ('2027-11-01', None)],
                  exact=['2027-05-15'], active=False),
    'TA13-pluto': dict(body='♇', aspect='⚹', point='☉', natal={'☉': 5.0, '☽': 105.0, '☿': 285.0}, target=305.0,
                       zone='UTC', periods=[('2026-12-19', '2027-02-23'), ('2027-07-27', None)],
                       exact=['2027-01-21', '2027-09-14'], active=False),
    'TA13-uranus': dict(body='♅', aspect='⚹', point='☉', natal={'☉': 5.0, '☽': 105.0, '☿': 285.0}, target=65.0,
                        zone='UTC', periods=[('2026-07-07', '2026-11-18'), ('2027-04-26', '2027-05-31')],
                        exact=['2026-07-31', '2026-10-22', '2027-05-14'], active=True),
    'TA13-saturn-minus': dict(body='♄', aspect='□', point='☽', natal={'☉': 5.0, '☽': 105.0, '☿': 285.0},
                              target=15.0, zone='UTC', periods=[('2027-03-09', '2027-03-25')],
                              exact=['2027-03-17'], active=False),
    'TA13-saturn-plus': dict(body='♄', aspect='□', point='☿', natal={'☉': 5.0, '☽': 105.0, '☿': 285.0},
                             target=15.0, zone='UTC', periods=[('2027-03-09', '2027-03-25')],
                             exact=['2027-03-17'], active=False),
}


def case_input(case):
    """(natal dict, target longitude) of a case, derived from the JPL table only."""
    if 't0' in case:
        target = J.lon_at(case['body'], case['t0'])
        return {case['point']: (target - ANGLES[case['aspect']]) % 360}, target
    if 'natal_at' in case:
        longitude = J.lon_at(case['body'], case['natal_at'])
        return {case['point']: longitude}, longitude
    natal = case['natal']
    target = case.get('target', (natal[case['point']] + ANGLES[case['aspect']]) % 360)
    return dict(natal), target


def reference_for(case):
    natal, target = case_input(case)
    start, end = reference_horizon(case['zone'])
    return J.reference_transit(J.Grid(case['body'], start, end), target, NOW)


def same_target(a, b):
    return abs(J.wrap(a - b)) < 1e-6


class ReferenceLiteralTests(unittest.TestCase):
    """Green on base: every expected literal is what the independent JPL pass gives (no prototype values)."""

    def test_case_literals_match_the_independent_jpl_pass(self):
        for name, case in CASES.items():
            with self.subTest(case=name):
                zone = ZoneInfo(case['zone'])
                found = reference_for(case)
                self.assertIsNotNone(found)
                self.assertEqual([(iso(a, zone), iso(b, zone)) for a, b in found['periods']],
                                 [tuple(p) for p in case['periods']])
                self.assertEqual([iso(x, zone) for x in found['exact']], case['exact'])
                self.assertEqual(found['orb_now'] is not None, case['active'])
                self.assertLessEqual(len(found['exact']), 3)
                self.assertFalse(found['station_touch'] or found['near_orb_edge'])

    def test_constructed_moments_respect_the_design_distances(self):
        """DESIGN §5: t0 at least max(20 min, 3·2″/speed) from local midnight and ≥ 10 days from a station."""
        for name, case in CASES.items():
            if 't0' not in case:
                continue
            with self.subTest(case=name):
                zone, t0, body = ZoneInfo(case['zone']), case['t0'], case['body']
                speed = abs(J.speed_at(body, t0))
                need = max(timedelta(minutes=20), timedelta(days=3 * 2 * ARCSEC / speed))
                local = t0.astimezone(zone)
                since = local - local.replace(hour=0, minute=0, second=0, microsecond=0)
                self.assertGreaterEqual(min(since, timedelta(days=1) - since), need)
                start, end = reference_horizon(case['zone'])
                for station, _, _ in J.Grid(body, start, end).stations:
                    self.assertGreater(abs(station - t0), timedelta(days=10))

    def test_dst_cases_differ_from_a_fixed_offset(self):
        """TA4/TA5 are only meaningful if a constant standard offset would give another date."""
        self.assertEqual(iso(CASES['TA4']['t0'], ZoneInfo('Etc/GMT+5')), '2027-03-14')
        self.assertEqual(iso(CASES['TA5']['t0'], ZoneInfo('Etc/GMT-10')), '2027-10-03')
        self.assertEqual(iso(CASES['TA3']['t0'], ZoneInfo('UTC')), '2027-03-10')


class AccuracyBase(unittest.TestCase):
    def setUp(self):
        self.transits = require_transits(self)

    def horizon(self, zone, now=NOW):
        return self.transits.horizon(ZoneInfo(zone), now)

    def pick(self, rows, body, aspect, point, target):
        found = [t for t in rows if (t.body, t.aspect, t.point) == (body, aspect, point) and same_target(t.target, target)]
        self.assertEqual(len(found), 1, f'{body} {aspect} {point} target {target:.6f}: {len(found)} rows')
        return found[0]

    def assert_periods(self, transit, reference, expected, zone):
        self.assertEqual(len(transit.periods), len(expected))
        for (start, end), ref, literal in zip(transit.periods, reference['periods'], expected):
            for got, ref_moment, want in ((start, ref[0], literal[0]), (end, ref[1], literal[1])):
                if want is None:
                    self.assertIsNone(got)
                    continue
                self.assertIsNotNone(got)
                if near_midnight(ref_moment, zone):
                    shift = abs(date.fromisoformat(iso(got, zone)) - date.fromisoformat(want))
                    self.assertLessEqual(shift, timedelta(days=1))
                else:
                    self.assertEqual(iso(got, zone), want)

    def assert_exact(self, transit, target, expected, zone):
        self.assertEqual([iso(x, zone) for x in transit.exact], expected)
        for moment in transit.exact:
            self.assertLessEqual(J.separation(transit.body, moment, target), EXACT_TOLERANCE,
                                 f'{transit.body} at {moment.isoformat()}')


class SkyNowTests(AccuracyBase):
    def test_ta1_sky_now_positions_and_retrograde_against_jpl(self):
        sky = self.transits.sky_now(NOW)
        self.assertEqual([s.body for s in sky], list(SLOW))
        self.assertEqual([s.retrograde for s in sky], [False, True, True, True, False])
        for item in sky:
            with self.subTest(body=item.body):
                reference = J.lon_at(item.body, NOW)
                self.assertLessEqual(abs(J.wrap(item.longitude - reference)), POSITION_TOLERANCE)
                self.assertTrue(0 <= item.longitude < 360)
                self.assertEqual(item.retrograde, J.speed_at(item.body, NOW) < 0)
        neptune = sky[3].longitude
        self.assertEqual(int(neptune // 30), 0, 'Neptune is in Aries, just past 0°')
        self.assertLess(neptune, 3)


class ExactDateTests(AccuracyBase):
    def run_case(self, name):
        case = CASES[name]
        zone = ZoneInfo(case['zone'])
        natal, target = case_input(case)
        hz = self.horizon(case['zone'])
        rows = self.transits.find_transits(natal, hz)
        transit = self.pick(rows, case['body'], case['aspect'], case['point'], target)
        reference = reference_for(case)
        self.assertTrue(same_target(transit.target, target))
        self.assertLessEqual(len(transit.exact), 3)
        self.assert_periods(transit, reference, case['periods'], zone)
        self.assert_exact(transit, target, case['exact'], zone)
        if case['active']:
            self.assertIsNotNone(transit.orb_now)
            self.assertLessEqual(abs(transit.orb_now - reference['orb_now']), POSITION_TOLERANCE)
        else:
            self.assertIsNone(transit.orb_now)
        return transit

    def test_ta2_uranus_square_ordinary_date(self):
        self.run_case('TA2')

    def test_ta3_saturn_conjunction_local_date_is_the_day_before_utc(self):
        self.run_case('TA3')

    def test_ta4_saturn_opposition_on_the_dst_change_day(self):
        self.run_case('TA4')

    def test_ta5_jupiter_trine_southern_hemisphere_dst_start(self):
        self.run_case('TA5')

    def test_ta6_neptune_sextile_sao_paulo_without_dst(self):
        self.run_case('TA6')

    def test_ta7_pluto_sextile_kathmandu_plus_0545(self):
        self.run_case('TA7')

    def test_ta8_jupiter_loop_three_exact_dates_two_periods_active_now(self):
        transit = self.run_case('TA8')
        self.assertEqual(len(transit.exact), 3)
        self.assertEqual(len(transit.periods), 2)

    def test_ta9_pluto_two_exact_dates_around_the_station_plus_january(self):
        self.run_case('TA9')

    def test_ta10_jupiter_period_without_a_touch_has_no_exact_date(self):
        transit = self.run_case('TA10')
        first, second = transit.periods
        self.assertFalse(any(first[0] <= x <= first[1] for x in transit.exact))
        self.assertEqual(len(transit.exact), 1)

    def test_ta11_past_exact_date_inside_the_active_period_is_kept(self):
        transit = self.run_case('TA11')
        self.assertLess(transit.exact[0], NOW)
        self.assertEqual(round(transit.orb_now, 1), 0.5)
        start, end = transit.periods[0]
        self.assertTrue(start <= NOW <= end)

    def test_ta12_horizon_edge_keeps_last_day_and_drops_the_day_after(self):
        self.run_case('TA12a')
        self.run_case('TA12b')

    def test_ta13_wrap_through_zero_and_both_sides_of_an_aspect(self):
        for name in ('TA13-pluto', 'TA13-uranus', 'TA13-saturn-minus', 'TA13-saturn-plus'):
            with self.subTest(case=name):
                self.run_case(name)


class RandomChartTests(AccuracyBase):
    """TA14: 50 random natal sets (fixed seed), every transit compared with the independent JPL pass."""
    SEED = 20261114
    CHARTS = 50

    def borderline(self, grid, target, start, end):
        """Cases where a 2″ ephemeris difference may legitimately add or drop a transit, period or root."""
        for _, _, lon in grid.stations:
            distance = float(abs(J.wrap(lon - target)))
            if distance < 30 * ARCSEC or abs(distance - J.ORB) < 10 * ARCSEC:
                return True
        for moment in (NOW, start, end):
            distance = float(abs(J.wrap(J.lon_at(grid.body, moment) - target)))
            if abs(distance - J.ORB) < abs(J.speed_at(grid.body, moment)) * 3 / 24 + 3 * ARCSEC:
                return True
        return False

    def test_ta14_random_charts_against_independent_jpl_pass(self):
        generator = random.Random(self.SEED)
        start, end = reference_horizon('UTC')
        hz = self.horizon('UTC')
        grids = {body: J.Grid(body, start, end) for body in SLOW}
        compared = skipped = 0
        for chart in range(self.CHARTS):
            natal = {point: round(generator.uniform(0, 360), 4) for point in POINTS}
            rows = self.transits.find_transits(natal, hz)
            got = {}
            for t in rows:
                key = (t.body, t.aspect, t.point, round(t.target % 360, 6))
                self.assertNotIn(key, got, f'chart {chart}: duplicate transit {key}')
                got[key] = t
                self.assertLessEqual(len(t.exact), 3, f'chart {chart}: {key}')
                self.assertTrue(t.periods, f'chart {chart}: {key} has no period')
                for begin, finish in t.periods:
                    self.assertTrue(finish is None or finish >= NOW, f'chart {chart}: {key} period ended before now')
                for x in t.exact:
                    self.assertTrue(start - timedelta(minutes=1) <= x <= end + timedelta(minutes=1))
                    self.assertTrue(any((b is None or b - timedelta(hours=1) <= x) and
                                        (e is None or x <= e + timedelta(hours=1)) for b, e in t.periods))
            expected = {}
            for point, longitude in natal.items():
                for aspect, target in J.targets(longitude, point):
                    for body, grid in grids.items():
                        found = J.reference_transit(grid, target, NOW)
                        if found is not None:
                            expected[(body, aspect, point, round(target, 6))] = (found, grid)
            for key, (reference, grid) in expected.items():
                with self.subTest(chart=chart, transit=key):
                    if self.borderline(grid, key[3], start, end):
                        skipped += 1
                        continue
                    compared += 1
                    self.assertIn(key, got, 'transit lost')
                    transit = got[key]
                    self.assertEqual(len(transit.periods), len(reference['periods']))
                    for mine, theirs in zip(transit.periods, reference['periods']):
                        for a, b in zip(mine, theirs):
                            self.assertEqual(a is None, b is None)
                            if b is not None:
                                slack = timedelta(hours=1) + timedelta(days=3 * ARCSEC / max(abs(J.speed_at(key[0], b)), 1e-6))
                                self.assertLessEqual(abs(a - b), slack)
                    self.assertEqual(len(transit.exact), len(reference['exact']))
                    for mine, theirs in zip(sorted(transit.exact), reference['exact']):
                        self.assertLessEqual(J.separation(key[0], mine, key[3]), EXACT_TOLERANCE)
                        slack = timedelta(minutes=1) + timedelta(days=3 * ARCSEC / max(abs(J.speed_at(key[0], theirs)), 1e-6))
                        self.assertLessEqual(abs(mine - theirs), slack)
                    if reference['orb_now'] is None:
                        self.assertIsNone(transit.orb_now)
                    else:
                        self.assertLessEqual(abs(transit.orb_now - reference['orb_now']), POSITION_TOLERANCE)
            for key in got.keys() - expected.keys():
                with self.subTest(chart=chart, extra=key):
                    self.assertTrue(self.borderline(grids[key[0]], key[3], start, end), 'transit not in the JPL pass')
        print(f'\nTA14: {compared} transits compared with JPL, {skipped} borderline skipped')
        self.assertGreater(compared, 300)
        self.assertLess(skipped, compared / 10)


class HorizonTests(AccuracyBase):
    """TA15: day boundary in the chart's zone, leap day; no ephemeris involved."""

    def test_ta15_horizon_fields(self):
        cases = (
            ('Europe/Kyiv', NOW, date(2026, 11, 14), date(2027, 11, 14), utc(2027, 11, 14, 22)),
            ('Asia/Tokyo', utc(2026, 11, 14, 23, 30), date(2026, 11, 15), date(2027, 11, 15), utc(2027, 11, 15, 15)),
            ('America/Los_Angeles', utc(2026, 11, 14, 23, 30), date(2026, 11, 14), date(2027, 11, 14),
             utc(2027, 11, 15, 8)),
            ('America/New_York', NOW, date(2026, 11, 13), date(2027, 11, 13), utc(2027, 11, 14, 5)),
            ('UTC', utc(2028, 2, 29, 12), date(2028, 2, 29), date(2029, 2, 28), utc(2029, 3, 1)),
        )
        for zone, now, today, last_day, end in cases:
            with self.subTest(zone=zone, now=now.isoformat()):
                with unittest.mock.patch('swisseph.calc_ut', side_effect=AssertionError('horizon needs no ephemeris')):
                    hz = self.horizon(zone, now)
                self.assertEqual(hz.zone, ZoneInfo(zone))
                self.assertEqual(J.as_utc(hz.now), now)
                self.assertEqual(hz.today, today)
                self.assertEqual(hz.last_day, last_day)
                self.assertIsNotNone(hz.start.tzinfo)
                self.assertIsNotNone(hz.end.tzinfo)
                self.assertEqual(J.as_utc(hz.end), end)
                self.assertEqual(J.as_utc(hz.start), now - timedelta(days=366))
                self.assertEqual(J.as_utc(hz.end).astimezone(ZoneInfo(zone)).time(), datetime.min.time())


class SpeedTests(AccuracyBase):
    def test_ta14_one_random_chart_is_computed_quickly(self):
        """Not a stand measurement (that is RESULT.md, p95 ≤ 150 ms); only catches an order-of-magnitude slip."""
        hz = self.horizon('Europe/Kyiv')
        natal = {point: (i * 29.3 + 7.1) % 360 for i, point in enumerate(POINTS)}
        started = time.monotonic()
        self.transits.find_transits(natal, hz)
        self.assertLess(time.monotonic() - started, 2.0)


if __name__ == '__main__':
    unittest.main()
