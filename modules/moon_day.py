"""The Moon of one local day against saved natal points (LUNAR-1 DESIGN §2, DECISIONS №30, №26).

Pure calculation: no database, no network, no AI. Natal longitudes come from the saved chart and are never
recalculated. Positions come from the same Swiss Ephemeris call and flags as the natal chart (transits.FLAGS).
The day is [local 00:00, next local 00:00) in the given zone; an event exactly at the end belongs to the next day.
"""
from collections import namedtuple
from datetime import datetime, time, timedelta

import swisseph as swe

from modules.synastry import ANGLES, MAJOR_ASPECTS, PLANETS, valid_longitude
from modules.transits import FLAGS, UTC, as_utc, _jd

POINTS = PLANETS + ANGLES   # ☉…♇, AS, MC: also the order of aspects that share a moment
QUARTERS = (("new_moon", 0), ("first_quarter", 90), ("full_moon", 180), ("last_quarter", 270))
HALVES = ("waxing_crescent", "waxing_gibbous", "waning_gibbous", "waning_crescent")
EXACT_SECONDS = 1           # bisection until the bracket is at most 1 s (the Moon moves about 0.55″ in it)

MoonDay = namedtuple("MoonDay", "date zone phase_code phase_exact illumination sign_index ingress aspects")


class MissingPositions(Exception):
    """The saved chart has no whole natal point to aspect."""


def natal_points(bodies):
    """Saved longitudes of POINTS; a broken point is skipped, none at all raises MissingPositions."""
    bodies = bodies if isinstance(bodies, dict) else {}
    natal = {}
    for symbol in POINTS:
        body = bodies.get(symbol)
        if isinstance(body, dict) and valid_longitude(body.get("degree")):
            natal[symbol] = body["degree"]
    if not natal:
        raise MissingPositions()
    return natal


def _longitude(body_id, jd):
    return swe.calc_ut(jd, body_id, FLAGS)[0][0] % 360


def _moon(jd):
    return _longitude(swe.MOON, jd)


def _elongation(jd):
    return (_longitude(swe.MOON, jd) - _longitude(swe.SUN, jd)) % 360


class _Sweep:
    """A growing angle over the day. Its sweep (f(t) - f(start)) mod 360 is monotonic, as the Moon and the
    Moon-Sun elongation only grow and move far less than 360° a day."""

    def __init__(self, angle, start_jd, length):
        self.angle, self.start_jd, self.length = angle, start_jd, length
        self.base = angle(start_jd)
        self.total = (angle(start_jd + length / 86400) - self.base) % 360

    def _ahead(self, seconds):
        return (self.angle(self.start_jd + seconds / 86400) - self.base) % 360

    def crossing(self, target):
        """Seconds after the start when the angle passes `target`, or None if not in [start, end)."""
        ahead = (target - self.base) % 360
        if ahead >= self.total:
            return None
        low, high = 0.0, self.length
        while high - low > EXACT_SECONDS:
            middle = (low + high) / 2
            if self._ahead(middle) < ahead:
                low = middle
            else:
                high = middle
        return (low + high) / 2


def _targets(longitude):
    for aspect, angle in MAJOR_ASPECTS:
        yield aspect, (longitude + angle) % 360
        if angle not in (0, 180):
            yield aspect, (longitude - angle) % 360


def calculate(natal, zone, now):
    """Moon day for the local date of `now` in `zone`; ephemeris errors propagate."""
    today = as_utc(now).astimezone(zone).date()
    # A missing local midnight (a DST jump at 00:00) resolves to the moment of the jump.
    start = datetime.combine(today, time.min, zone).astimezone(UTC)
    end = datetime.combine(today + timedelta(days=1), time.min, zone).astimezone(UTC)
    noon = datetime.combine(today, time(12), zone).astimezone(UTC)
    start_jd, length = _jd(start), (end - start).total_seconds()

    def moment(seconds):
        return start + timedelta(seconds=seconds)

    moon = _Sweep(_moon, start_jd, length)
    elongation = _Sweep(_elongation, start_jd, length)

    phase_code, phase_exact = None, None
    for code, angle in QUARTERS:     # the elongation moves about 12° a day: at most one quarter
        seconds = elongation.crossing(angle)
        if seconds is not None:
            phase_code, phase_exact = code, moment(seconds)
            break
    if phase_code is None:
        phase_code = HALVES[int(_elongation(_jd(noon)) // 90) % 4]
    illumination = swe.pheno_ut(_jd(noon), swe.MOON, FLAGS)[1]

    sign_index = int(moon.base // 30) % 12
    ingress = None
    for index in range(12):          # the Moon moves at most about 16° a day: at most one ingress
        seconds = moon.crossing(30 * index)
        if seconds is not None:
            ingress = (moment(seconds), index)
            break

    order = {point: rank for rank, point in enumerate(POINTS)}
    angles = dict(MAJOR_ASPECTS)
    found = []
    for point, longitude in natal.items():
        for aspect, target in _targets(longitude):
            seconds = moon.crossing(target)
            if seconds is not None:
                found.append((seconds, order[point], angles[aspect], aspect, point))
    found.sort()
    aspects = [(moment(seconds), aspect, point) for seconds, _, _, aspect, point in found]
    return MoonDay(today, zone, phase_code, phase_exact, illumination, sign_index, ingress, aspects)
