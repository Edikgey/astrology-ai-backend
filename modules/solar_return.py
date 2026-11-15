"""The solar return: the year chart from the birthday (SOLAR-1 DESIGN §2, DECISIONS №30, №26, №3).

Pure calculation: no database, no network, no AI. The natal Sun comes from the saved chart and is never recalculated.
The moment is the library's Sun crossing (swisseph.solcross_ut, same flags as the natal chart); the year chart is the
existing Ephemeris (planets and Placidus cusps) and the existing synastry.aspect_rows, called read-only.
"""
from collections import namedtuple

import swisseph as swe

from modules.ephemeris import Ephemeris
from modules.moon_day import POINTS, MissingPositions
from modules.synastry import aspect_rows, valid_longitude
from modules.transits import FLAGS, as_utc, _jd, _moment

BODIES = (("☉", "Солнце"), ("☽", "Луна"), ("☿", "Меркурий"), ("♀", "Венера"), ("♂", "Марс"),
          ("♃", "Юпитер"), ("♄", "Сатурн"), ("♅", "Уран"), ("♆", "Нептун"), ("♇", "Плутон"))
FAST = ("☉", "☽", "☿", "♀", "♂", "AS", "MC")   # a pair needs one of these: slow planets between themselves are generational
STRONGEST = 3
PREVIOUS_DAYS = 366                             # more than one year between returns, less than two

SolarYear = namedtuple("SolarYear", "start next points ascendant_sign sun_house moon_sign moon_house aspects")


def natal_sun(bodies):
    """Saved longitude of the natal Sun; missing or broken raises MissingPositions."""
    sun = bodies.get("☉") if isinstance(bodies, dict) else None
    if not isinstance(sun, dict) or not valid_longitude(sun.get("degree")):
        raise MissingPositions()
    return sun["degree"]


def year_bounds(sun, now):
    """(start, next) aware UTC: the last return before `now` and the first one at `now` or later."""
    following = swe.solcross_ut(sun, _jd(now), FLAGS)
    previous = swe.solcross_ut(sun, following - PREVIOUS_DAYS, FLAGS)
    return _moment(previous), _moment(following)


def strongest(points):
    """Up to three (a, aspect, b) by orb, then point order: no AS-MC pair, no pair of slow planets only."""
    order = [symbol for symbol in POINTS if symbol in points]
    found = []
    for i, a in enumerate(order):
        for j in range(i + 1, len(order)):
            b = order[j]
            if {a, b} == {"AS", "MC"} or not (a in FAST or b in FAST):
                continue
            for row in aspect_rows({a: points[a]}, {b: points[b]}):
                found.append((row["orb_deg"], POINTS.index(a), POINTS.index(b), a, row["aspect"], b))
    found.sort(key=lambda row: row[:3])
    return [(a, aspect, b) for _, _, _, a, aspect, b in found[:STRONGEST]]


def calculate(sun, lat, lon, now):
    """The solar year around `now` for the place (lat, lon); ephemeris errors propagate."""
    start, following = year_bounds(sun, as_utc(now))
    hour = start.hour + start.minute / 60 + (start.second + start.microsecond / 1e6) / 3600
    chart = Ephemeris(start.year, start.month, start.day, hour, lon, lat)   # longitude first, as endpoints.py
    planets = chart.get_planets()
    cusps = chart.get_house_cusps()
    points = {symbol: planets[name]["абс_долгота"] % 360 for symbol, name in BODIES}   # the ten bodies
    angles = {"AS": cusps[0]["degree"] % 360, "MC": cusps[9]["degree"] % 360}
    return SolarYear(start, following, points,
                     ascendant_sign=int(angles["AS"] // 30), sun_house=int(planets["Солнце"]["дом"]),
                     moon_sign=int(points["☽"] // 30), moon_house=int(planets["Луна"]["дом"]),
                     aspects=strongest({**points, **angles}))
