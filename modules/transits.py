"""Transits of Jupiter..Pluto to saved natal points (TRANSITS-1 DESIGN §1, DECISIONS №25, №26).

Pure calculation: no database, no network. Natal longitudes come from the saved chart and are never
recalculated. Positions come from the same Swiss Ephemeris call and flags as the natal chart.
"""
from bisect import bisect_left
from collections import namedtuple
from datetime import date, datetime, time, timedelta, timezone
import math
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import swisseph as swe

from modules.synastry import MAJOR_ASPECTS

BODIES = (("♃", swe.JUPITER), ("♄", swe.SATURN), ("♅", swe.URANUS), ("♆", swe.NEPTUNE), ("♇", swe.PLUTO))
FLAGS = swe.FLG_SWIEPH | swe.FLG_SPEED
ORB = 1                     # degrees, every body and aspect (DECISIONS №26)
STEP_DAYS = 4               # grid step; refinement below is by bisection on calc_ut
EXACT_DAYS = 1 / 1440       # exact moments and stations to <= 1 min
EDGE_DAYS = 1 / 48          # period boundaries to <= 30 min (DESIGN allows 1 h)
UNIX_EPOCH_JD = 2440587.5
UTC = timezone.utc

Horizon = namedtuple("Horizon", "zone now today last_day start end")
SkyBody = namedtuple("SkyBody", "body longitude retrograde")
Transit = namedtuple("Transit", "body aspect point target orb_now periods exact")


def server_now():
    """The only clock for "now" (aware UTC); tests patch it."""
    return datetime.now(UTC)


def as_utc(moment):
    """Contract datetimes are aware UTC; a naive value is read as UTC."""
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)


def verified_zone(chart):
    """IANA zone of a chart whose birth moment was converted with a known zone; legacy charts give None."""
    if not isinstance(chart.birth_utc, datetime) or not isinstance(chart.timezone, str) or not chart.timezone:
        return None
    try:
        return ZoneInfo(chart.timezone)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return None


def horizon(zone, now):
    """Local today in the chart's zone, the same day next year and the UTC bounds of the search."""
    now = as_utc(now)
    today = now.astimezone(zone).date()
    try:
        last_day = today.replace(year=today.year + 1)
    except ValueError:              # 29 February -> 28 February
        last_day = date(today.year + 1, 2, 28)
    end = datetime.combine(last_day + timedelta(days=1), time.min, zone).astimezone(UTC)
    return Horizon(zone, now, today, last_day, now - timedelta(days=366), end)


def _jd(moment):
    return UNIX_EPOCH_JD + as_utc(moment).timestamp() / 86400


def _moment(jd):
    return datetime(1970, 1, 1, tzinfo=UTC) + timedelta(days=jd - UNIX_EPOCH_JD)


def _wrap(value):
    """Signed difference in [-180, 180)."""
    return (value + 180) % 360 - 180


def _position(body_id, jd):
    values, _ = swe.calc_ut(jd, body_id, FLAGS)
    return values[0] % 360, values[3]


def sky_now(now):
    jd = _jd(now)
    result = []
    for symbol, body_id in BODIES:
        longitude, speed = _position(body_id, jd)
        result.append(SkyBody(symbol, longitude, speed < 0))
    return result


class _Segment:
    """Part of a body's path between stations: the unwrapped longitude is monotonic in it."""

    def __init__(self, nodes):
        self.jds = [jd for jd, _ in nodes]
        self.lons = [lon for _, lon in nodes]
        self.increasing = self.lons[-1] >= self.lons[0]
        self.keys = self.lons if self.increasing else [-lon for lon in self.lons]
        self.low, self.high = min(self.lons[0], self.lons[-1]), max(self.lons[0], self.lons[-1])


class _Track:
    """Grid of one body over the horizon, split at stations into monotonic segments."""

    def __init__(self, body_id, start_jd, end_jd):
        self.body_id = body_id
        count = math.ceil((end_jd - start_jd) / STEP_DAYS)
        jds = [start_jd + k * STEP_DAYS for k in range(count)] + [end_jd]
        parts, nodes, previous = [], [], None
        for jd in jds:
            longitude, speed = _position(body_id, jd)
            lon = longitude if previous is None else previous[1] + _wrap(longitude - previous[1])
            if previous is not None and (previous[2] < 0) != (speed < 0):
                station = self._station(previous[0], jd, previous[2] < 0)
                nodes.append((station, self._unwrapped(station, previous[1])))
                parts.append(nodes)
                nodes = [nodes[-1]]
            nodes.append((jd, lon))
            previous = (jd, lon, speed)
        self.segments = [_Segment(part) for part in parts + [nodes]]
        self.low = min(segment.low for segment in self.segments)
        self.high = max(segment.high for segment in self.segments)

    def _unwrapped(self, jd, near):
        return near + _wrap(_position(self.body_id, jd)[0] - near)

    def _station(self, a, b, retrograde_at_a):
        while b - a > EXACT_DAYS:
            middle = (a + b) / 2
            if (_position(self.body_id, middle)[1] < 0) == retrograde_at_a:
                a = middle
            else:
                b = middle
        return (a + b) / 2

    def _cross(self, segment, level, tolerance):
        """Moment where the monotonic segment passes the unwrapped longitude `level`."""
        key = level if segment.increasing else -level
        k = min(max(bisect_left(segment.keys, key), 1), len(segment.jds) - 1)
        a, b, near = segment.jds[k - 1], segment.jds[k], segment.lons[k - 1]
        while b - a > tolerance:
            middle = (a + b) / 2
            if (self._unwrapped(middle, near) < level) == segment.increasing:
                a = middle
            else:
                b = middle
        return (a + b) / 2

    def spans(self, target):
        """Orb periods [start, end] (Julian days; None = beyond the horizon) and exact moments for one target."""
        pieces, exact = [], []
        for segment in self.segments:
            first = math.ceil((segment.low - ORB - target) / 360)
            last = math.floor((segment.high + ORB - target) / 360)
            for turn in range(first, last + 1):
                level = target + 360 * turn
                low, high = level - ORB, level + ORB
                begin_lon, finish_lon = segment.lons[0], segment.lons[-1]
                entry = (segment.jds[0] if low <= begin_lon <= high else
                         self._cross(segment, low if segment.increasing else high, EDGE_DAYS))
                leave = (segment.jds[-1] if low <= finish_lon <= high else
                         self._cross(segment, high if segment.increasing else low, EDGE_DAYS))
                pieces.append([entry, leave])
                if segment.low <= level <= segment.high:
                    exact.append(self._cross(segment, level, EXACT_DAYS))
        pieces.sort()
        periods = []
        for entry, leave in pieces:
            if periods and periods[-1][1] == entry:     # the same station closes one piece and opens the next
                periods[-1][1] = leave
            else:
                periods.append([entry, leave])
        exact.sort()
        exact = [x for i, x in enumerate(exact) if i == 0 or x - exact[i - 1] > EXACT_DAYS]
        return periods, exact


def _targets(longitude):
    for aspect, angle in MAJOR_ASPECTS:
        yield aspect, (longitude + angle) % 360
        if angle not in (0, 180):
            yield aspect, (longitude - angle) % 360


def find_transits(natal, hz):
    """Transits whose orb period intersects [now, end]; ephemeris errors propagate, no network."""
    start_jd, end_jd, now_jd = _jd(hz.start), _jd(hz.end), _jd(hz.now)
    result = []
    for symbol, body_id in BODIES:
        track = _Track(body_id, start_jd, end_jd)
        longitude_now = _position(body_id, now_jd)[0]
        for point, natal_longitude in natal.items():
            for aspect, target in _targets(natal_longitude):
                if math.floor((track.high + ORB - target) / 360) < math.ceil((track.low - ORB - target) / 360):
                    continue
                periods, exact = track.spans(target)
                kept = [(a, b) for a, b in periods if b >= now_jd]
                if not kept:
                    continue
                exact = [x for x in exact if any(a <= x <= b for a, b in kept)]
                orb_now = abs(_wrap(longitude_now - target))
                result.append(Transit(
                    body=symbol, aspect=aspect, point=point, target=target,
                    orb_now=orb_now if orb_now <= ORB else None,
                    periods=[(None if a <= start_jd else _moment(a), None if b >= end_jd else _moment(b))
                             for a, b in kept],
                    exact=[_moment(x) for x in exact]))
    return result
