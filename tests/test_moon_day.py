"""LUNAR-1-be API regression LD1, LD2 (tasks/LUNAR-1/DESIGN.md sections 3, 4, 7): GET /natal-chart/{chart_id}/moon-day.

Real application, isolated SQLite (the fixtures of tests/test_my_charts.py), synthetic data only. The only clock is
modules.transits.server_now, patched here. No network, no .secrets, no OpenAI: the endpoint must not touch the AI chat.
The accuracy of the calculation itself is checked against JPL in tests/test_moon_day_jpl.py.
"""
import logging
import re
import time
import unittest
from contextlib import ExitStack, contextmanager
from datetime import date, datetime, timezone
from unittest.mock import patch

import swisseph

import test_chat_quality as cq
import test_moon_day_jpl as J
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
MOON_LEGACY_CHART = ('Луну на сегодня для этой карты не посчитать: она построена в старой версии Lunaria, '
                     'где часовой пояс рождения не проверялся. Постройте карту заново, выбрав место рождения '
                     'из подсказок, и откройте «Луну сегодня» для новой карты.')
MOON_UNAVAILABLE = 'Сейчас не получилось рассчитать Луну на сегодня. Попробуйте ещё раз через несколько минут.'
FAILED_LINE = re.compile(r'^Moon day outcome=failed reason=(ephemeris_error|missing_saved_positions|unclassified) '
                         r'duration_ms=\d+$')
SLOW_LINE = re.compile(r'^Moon day outcome=slow duration_ms=\d+$')
KEYS = {'date', 'timezone', 'phase', 'sign', 'aspects'}

NOW_A = J.noon_now('A')                           # 2026-11-15 12:00 Europe/Kyiv
NOW_NEW_MOON = datetime(2026, 12, 9, 3, 0, tzinfo=UTC)   # 22:00 on 8 Dec in New York, 05:00 on 9 Dec in Kyiv
NATAL_BODIES_A = {symbol: cq.body(symbol, longitude) for symbol, longitude in J.NATAL_A.items()}
EXPECTED_A = {
    'date': '2026-11-15', 'timezone': 'Europe/Kyiv',
    'phase': {'code': 'waxing_crescent', 'illumination': 31, 'exact_at': None},
    'sign': {'index': 9, 'ingress': {'at': '11:24', 'index': 10}},
    'aspects': [{'at': '03:17', 'aspect': '△', 'point': '♀'}, {'at': '09:41', 'aspect': '□', 'point': 'MC'},
                {'at': '16:05', 'aspect': '☍', 'point': '♄'}, {'at': '23:59', 'aspect': '⚹', 'point': '☉'}],
}


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


def moon_lines(lines):
    return [line for line in lines if line.startswith('Moon day')]


class MoonDayApiBase(cq.ChatQualityBase):
    def setUp(self):
        super().setUp()
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.now = self.stack.enter_context(patch.object(transits, 'server_now', return_value=NOW_A))

    def freeze(self, moment):
        self.now.return_value = moment

    def verified(self, bodies=None, timezone=CHART_ZONE, **fields):
        cid = self.chart()
        self.store(cid, NATAL_BODIES_A if bodies is None else bodies, birth_utc=BIRTH_UTC, timezone=timezone, **fields)
        return cid

    def get(self, cid, headers='owner', **params):
        headers = self.owner if headers == 'owner' else headers
        return self.client.get(f'/natal-chart/{cid}/moon-day', headers=headers, params=params or None)

    def rows(self):
        with self.sessions() as db:
            return (db.query(GPTUsage).count(), db.query(GPTMessage).count(), db.query(NatalChart).count(),
                    [row.bodies_for_circle for row in db.query(ChartData).order_by(ChartData.chart_id)])


class ContractAndRefusalTests(MoonDayApiBase):
    def test_ld1_success_has_exactly_the_contract_keys_and_the_reference_values(self):
        cid = self.verified()
        before = self.rows()
        response = self.get(cid, tz='Europe/Kyiv')
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(set(body), KEYS)
        self.assertEqual(set(body['phase']), {'code', 'illumination', 'exact_at'})
        self.assertEqual(set(body['sign']), {'index', 'ingress'})
        self.assertEqual(set(body['sign']['ingress']), {'at', 'index'})
        self.assertTrue(all(set(row) == {'at', 'aspect', 'point'} for row in body['aspects']))
        self.assertIsInstance(body['phase']['illumination'], int)
        self.assertEqual(body, EXPECTED_A)            # day A of DESIGN section 5, JPL reference moments
        self.assertEqual(self.rows(), before)         # no GPTUsage, no GPTMessage, no chart row, saved data unchanged

    def test_ld1_event_day_has_exact_at_and_no_ingress_is_null(self):
        cid = self.verified()
        self.freeze(J.noon_now('Q3'))                 # last quarter 08:08 in Kyiv, no sign change
        body = self.get(cid, tz='Europe/Kyiv').json()
        self.assertEqual(body['phase'], {'code': 'last_quarter', 'illumination': 48, 'exact_at': '08:08'})
        self.assertEqual(body['sign'], {'index': 5, 'ingress': None})

    def test_ld1_authentication_and_ownership_are_decided_by_the_server(self):
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
        other = self.client.get(f'/natal-chart/{own}/moon-day', headers=self.other)
        self.assertEqual((other.status_code, other.json()['detail']), (403, CHART_FORBIDDEN))

    def test_ld1_legacy_chart_is_refused_without_any_ephemeris_call(self):
        calls = []
        real = swisseph.calc_ut

        def recording(*args, **kwargs):
            calls.append(args)
            return real(*args, **kwargs)

        variants = ({}, {'birth_utc': BIRTH_UTC, 'timezone': None}, {'birth_utc': BIRTH_UTC, 'timezone': ''},
                    {'birth_utc': BIRTH_UTC, 'timezone': 'Mars/Olympus_Mons'}, {'birth_utc': None, 'timezone': 'Europe/Kyiv'})
        for fields in variants:
            with self.subTest(fields=fields):
                cid = self.chart()
                self.store(cid, NATAL_BODIES_A, **fields)
                with patch.object(swisseph, 'calc_ut', recording):
                    response = self.get(cid, tz='Europe/Kyiv')
                self.assertEqual((response.status_code, response.json()['detail']), (422, MOON_LEGACY_CHART))
        self.assertEqual(calls, [])
        # A legacy chart is refused as legacy even if its saved data is also gone (DESIGN section 4 order).
        cid = self.chart(related=False)
        self.assertEqual(self.get(cid).status_code, 422)

    def test_ld1_missing_saved_positions_is_a_404_with_one_closed_log_line(self):
        no_data = self.chart(related=False)
        with self.sessions() as db:
            chart = db.get(NatalChart, no_data)
            chart.birth_utc, chart.timezone = BIRTH_UTC, CHART_ZONE
            db.commit()
        unusable = {symbol: cq.body(symbol, 10.0) for symbol in ('☉', '☽')}
        for symbol, value in (('☉', 360.0), ('☽', -1.0)):
            unusable[symbol]['degree'] = value
        unusable['MC'] = {'symbol': 'MC', 'degree': 'x'}
        for label, cid in (('no ChartData', no_data), ('no whole point', self.verified(bodies=unusable)),
                           ('empty', self.verified(bodies={}))):
            with self.subTest(case=label), captured_logs() as lines:
                response = self.get(cid, tz='Europe/Kyiv')
                self.assertEqual((response.status_code, response.json()['detail']), (404, CHART_DATA_MISSING))
                found = moon_lines(lines)
                self.assertEqual(len(found), 1, found)
                self.assertRegex(found[0], FAILED_LINE)
                self.assertIn('reason=missing_saved_positions ', found[0])

    def test_ld1_one_whole_point_is_enough(self):
        bodies = {'☉': cq.body('☉', J.NATAL_A['☉']), '☽': {'symbol': '☽', 'degree': 400.0},
                  '♀': {'symbol': '♀', 'degree': 'x'}, '♂': {'symbol': '♂'}, 'MC': None}
        cid = self.verified(bodies=bodies)
        response = self.get(cid, tz='Europe/Kyiv')
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body['aspects'], [{'at': '23:59', 'aspect': '⚹', 'point': '☉'}])
        self.assertEqual({key: body[key] for key in ('date', 'phase', 'sign')},
                         {key: EXPECTED_A[key] for key in ('date', 'phase', 'sign')})

    def test_ld1_calculation_failures_are_a_503_with_one_closed_log_line_and_no_leak(self):
        cid = self.verified()
        for error, reason in ((swisseph.Error('ephemeris file missing'), 'ephemeris_error'),
                              (RuntimeError(CANARY), 'unclassified')):
            with self.subTest(error=type(error).__name__):
                before = self.rows()
                with captured_logs() as lines, patch.object(swisseph, 'calc_ut', side_effect=error):
                    response = self.get(cid, tz='Europe/Kyiv')
                self.assertEqual(response.status_code, 503, response.text)
                self.assertEqual(response.json()['detail'], MOON_UNAVAILABLE)
                for leak in ('КАНАРЕЙКА', '7781', '1990-05-03', 'ephemeris file missing', 'Traceback'):
                    self.assertNotIn(leak, response.text)
                found = moon_lines(lines)
                self.assertEqual(len(found), 1, found)                     # exactly one line per request
                self.assertRegex(found[0], FAILED_LINE)
                self.assertIn(f'reason={reason} ', found[0])
                for line in lines:                                          # nothing else leaks either
                    for leak in ('КАНАРЕЙКА', '7781', '1990-05-03', 'ephemeris file missing', 'Europe/Kyiv', '2026-11-15'):
                        self.assertNotIn(leak, line)
                self.assertEqual(self.rows(), before)
        with captured_logs() as lines:                                      # recovery: the next request is fine
            self.assertEqual(self.get(cid, tz='Europe/Kyiv').status_code, 200)
        self.assertEqual(moon_lines(lines), [])                             # a fast success logs nothing

    def test_ld1_slow_success_logs_one_slow_line(self):
        cid = self.verified()
        real = swisseph.calc_ut
        state = {'slept': False}

        def slow_once(*args, **kwargs):
            if not state['slept']:
                state['slept'] = True
                time.sleep(0.6)
            return real(*args, **kwargs)

        with captured_logs() as lines, patch.object(swisseph, 'calc_ut', slow_once):
            response = self.get(cid, tz='Europe/Kyiv')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), EXPECTED_A)
        found = moon_lines(lines)
        self.assertEqual(len(found), 1, found)
        self.assertRegex(found[0], SLOW_LINE)

    def test_ld1_other_users_own_chart_works_and_the_ai_chat_is_untouched(self):
        cid = self.chart(user_id=2)
        self.store(cid, NATAL_BODIES_A, birth_utc=BIRTH_UTC, timezone='Europe/Kyiv')
        with self.sessions() as db:
            before = (db.query(GPTUsage).count(), db.query(GPTMessage).count())
        for _ in range(3):                                                  # no quota, no rate limit on reading
            response = self.client.get(f'/natal-chart/{cid}/moon-day', headers=self.other, params={'tz': 'Europe/Kyiv'})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json(), EXPECTED_A)
        with self.sessions() as db:
            self.assertEqual((db.query(GPTUsage).count(), db.query(GPTMessage).count()), before)


class LocalDayTests(MoonDayApiBase):
    """LD2: `now` is one instant; the local day follows the device zone (`tz`), else the chart's zone."""

    def setUp(self):
        super().setUp()
        self.freeze(NOW_NEW_MOON)
        self.cid = self.verified()

    def test_ld2_device_zone_decides_the_local_day(self):
        new_york = self.get(self.cid, tz='America/New_York').json()
        kyiv = self.get(self.cid, tz='Europe/Kyiv').json()
        utc_zone = self.get(self.cid, tz='UTC').json()
        self.assertEqual((new_york['date'], new_york['timezone']), ('2026-12-08', 'America/New_York'))
        self.assertEqual(new_york['phase']['code'], 'new_moon')
        self.assertEqual(new_york['phase']['exact_at'], '19:51')
        self.assertEqual((kyiv['date'], kyiv['timezone']), ('2026-12-09', 'Europe/Kyiv'))
        self.assertEqual(kyiv['phase']['code'], 'new_moon')
        self.assertEqual(kyiv['phase']['exact_at'], '02:51')
        self.assertEqual((utc_zone['date'], utc_zone['timezone'], utc_zone['phase']['exact_at']),
                         ('2026-12-09', 'UTC', '00:51'))

    def test_ld2_missing_or_unusable_tz_falls_back_to_the_chart_zone_without_any_422(self):
        for label, params in (('absent', {}), ('empty', {'tz': ''}), ('unknown', {'tz': 'Mars/Olympus'}),
                              ('traversal', {'tz': '../etc/passwd'}), ('65 chars', {'tz': 'A' * 65}),
                              ('not ascii', {'tz': 'Европа/Киев'}), ('spaces', {'tz': 'Europe/Kyiv; drop table'})):
            with self.subTest(tz=label):
                response = self.get(self.cid, **params)
                self.assertEqual(response.status_code, 200, response.text)
                body = response.json()
                self.assertEqual(body['timezone'], CHART_ZONE)                  # the zone the day was counted in
                self.assertEqual(body['date'], '2026-12-09')                    # 16:00 on 9 Dec in Auckland
                self.assertEqual(body['phase'], {'code': 'new_moon', 'illumination': 0, 'exact_at': '13:51'})

    def test_ld2_the_zone_is_chosen_per_request_and_not_remembered(self):
        for tz, day in (('America/New_York', '2026-12-08'), (None, '2026-12-09'), ('America/New_York', '2026-12-08')):
            params = {} if tz is None else {'tz': tz}
            self.assertEqual(self.get(self.cid, **params).json()['date'], day)


if __name__ == '__main__':
    unittest.main()
