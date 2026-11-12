"""Compact chart facts from existing calculator outputs; no interpretive inventions.

Only formats what is saved: degree in sign and cusps are formatted saved longitudes,
orbs are parsed from the saved aspect lines. Nothing is recalculated here.
"""
import json
import math
import re
from fastapi import HTTPException
from database.queries import NatalChart, ChartData
from modules.ephemeris import ASTEROID_SYMBOLS

CHART_CONTEXT_BUDGET = 24000  # UTF-8 bytes, within the shared 64000-byte total guard.

SIGNS = ("Овен", "Телец", "Близнецы", "Рак", "Лев", "Дева",
         "Весы", "Скорпион", "Стрелец", "Козерог", "Водолей", "Рыбы")
# Same names as the calculator's own tables (Aspects/Ephemeris keep them per instance).
BODY_NAMES = {
    "☉": "Солнце", "☽": "Луна", "☿": "Меркурий", "♀": "Венера", "♂": "Марс", "♃": "Юпитер",
    "♄": "Сатурн", "♅": "Уран", "♆": "Нептун", "♇": "Плутон", "☊": "Северный Узел", "☋": "Южный Узел",
    "⚳": "Церера", "⚴": "Паллада", "⚵": "Юнона", "⚶": "Веста", "⚷": "Хирон", "φ": "Фол",
    "⚸": "Черная Луна Лилит", "W": "Белая Луна Селена", "AS": "Асцендент", "DS": "Десцендент",
    "MC": "Средина Неба", "IC": "Надир", "Vx": "Вертекс", "X": "Часть Фортуны",
}
ASPECT_NAMES = {
    "☌": "соединение", "☍": "оппозиция", "△": "трин", "□": "квадрат", "⚹": "секстиль",
    "⚻": "квинконс", "∠": "полуквадрат", "bQ": "биквинтиль", "N": "новиль", "S": "септиль",
    "⚺": "полусекстиль", "Q": "квинтиль", "∠∠": "полутороквадрат",
}
MAJOR_ASPECTS = {"☌", "☍", "△", "□", "⚹"}
CORE_POINTS = {"☉", "☽", "AS", "MC", "☿", "♀", "♂"}
# Defining axes are always exact oppositions and carry no information.
AXES = {frozenset(("AS", "DS")), frozenset(("MC", "IC")), frozenset(("☊", "☋"))}
ORB_LINE = re.compile(r"^\s*(\S+)\s+(.+?)\s+орбис:\s*(\d+)°\s*(\d+)'\s*$")


def compact_json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _longitude(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return value % 360


def degree_in_sign(value):
    """Saved longitude as «23°05′»: minutes are truncated, so 29°34′ never shows as 30°."""
    longitude = _longitude(value)
    if longitude is None:
        return None
    # The epsilon absorbs float noise (23.1 stored as 23.0999...); the cap keeps it inside the sign.
    minutes = min(math.floor((longitude % 30) * 60 + 1e-8), 30 * 60 - 1)
    return f"{minutes // 60}°{minutes % 60:02d}′"


def sign_of(value):
    longitude = _longitude(value)
    return None if longitude is None else SIGNS[int(longitude // 30)]


def body_name(symbol):
    return BODY_NAMES.get(symbol, symbol)


def saved_orbs(structured, bodies):
    """Orbs from the saved text lines, keyed by (pair, aspect symbol); conflicting copies -> None."""
    aliases = {name.casefold(): symbol for symbol, name in BODY_NAMES.items()}
    aliases.update({str(body.get("label")).casefold(): symbol for symbol, body in bodies.items()
                    if isinstance(body, dict) and body.get("label")})
    orbs = {}
    lines = structured.values() if isinstance(structured, dict) else ()
    for line in (item for group in lines if isinstance(group, list) for item in group):
        match = ORB_LINE.match(line) if isinstance(line, str) else None
        if not match or match[1] not in ASPECT_NAMES:
            continue
        names = match[2].split(f" {ASPECT_NAMES[match[1]]} ")
        if len(names) != 2:
            continue
        pair = [aliases.get(name.replace(" (R)", "").strip().casefold()) for name in names]
        if None in pair:
            continue
        key = (frozenset(pair), match[1])
        orb = round(int(match[3]) + int(match[4]) / 60, 1)
        orbs[key] = orb if orbs.get(key, orb) == orb else None
    return orbs


def aspect_rows(aspects, structured, bodies):
    """One row per aspect, most important first: major with core points, other major,
    minor with core points, other minor; inside a group by orb, unknown orb last."""
    orbs = saved_orbs(structured, bodies)
    first = {}
    for aspect in aspects or []:
        pair = frozenset((aspect["from_body"], aspect["to_body"]))
        if pair not in AXES:
            first.setdefault((pair, aspect["aspect"]), (aspect["from_body"], aspect["aspect"], aspect["to_body"]))
    ranked = []
    for key, (a, symbol, b) in first.items():
        major = symbol in MAJOR_ASPECTS
        group = (0 if major else 2) + (0 if key[0] & CORE_POINTS else 1)
        orb = orbs.get(key)
        row = [body_name(a), ASPECT_NAMES.get(symbol, symbol), body_name(b), orb, "major" if major else "minor"]
        ranked.append(((group, orb is None, orb or 0, row[0], row[1], row[2]), row))
    return [row for _, row in sorted(ranked, key=lambda item: item[0])]


def build_chart_context(db, user_id, chart_id):
    chart = db.query(NatalChart).filter_by(id=chart_id, user_id=user_id).first()
    if chart is None or user_id is None:
        raise HTTPException(404, "Карта недоступна.")
    stored = db.query(ChartData).filter_by(chart_id=chart_id).first()
    if stored is None or not stored.bodies_for_circle:
        # Do not silently recalculate: the legacy asteroid calculator can make
        # external requests and disagree with the chart the user actually sees.
        raise HTTPException(409, "Расчётные данные карты отсутствуют. Обратитесь в поддержку для восстановления карты.")
    bodies = stored.bodies_for_circle
    placements = [[symbol, body.get("label"), body.get("sign"), degree_in_sign(body.get("degree")),
                   body.get("house"), body.get("retrograde")] for symbol, body in bodies.items()]
    cusps = stored.houses
    cusp_rows = ([[c.get("symbol"), sign_of(c.get("degree")), degree_in_sign(c.get("degree"))] for c in cusps]
                 if isinstance(cusps, list) and cusps and all(isinstance(c, dict) for c in cusps) else None)
    pattern_rows = []
    for pattern in stored.patterns_data or []:
        row = [pattern['type'], sorted({body_name(b['symbol']) for b in pattern['bodies']})]
        if row not in pattern_rows:
            pattern_rows.append(row)
    context = {
        # No coordinates or place names: the provider does not need them to read a calculated chart.
        "birth": {"date": [chart.year, chart.month, chart.day], "local_hour": chart.hour,
                  "timezone": chart.timezone,
                  "utc": chart.birth_utc.isoformat() + "Z" if chart.birth_utc else None},
        "placement_columns": ["body", "name", "sign", "degree_in_sign", "house", "retrograde"],
        "placements": placements,
        "core_bodies": [s for s in ("☉", "☽", "AS") if s in bodies],
        # Same rule as ephemeris.calculation_warnings, applied to the saved bodies.
        "not_calculated": [name for name, symbol in ASTEROID_SYMBOLS.items() if symbol not in bodies],
        "houses": {"system": stored.house_system, "cusps": cusp_rows},
        "aspect_columns": ["body_a", "aspect", "body_b", "orb_deg", "kind"],
        "aspects": aspect_rows(stored.aspects_for_circle, stored.aspects_structured, bodies),
        "pattern_columns": ["type", "bodies"], "patterns": pattern_rows,
        "omitted": {"aspects": 0},
        "limitations": "Only natal calculations; no transits." if chart.birth_utc and chart.timezone else
            "Legacy chart: birth timezone/minutes unverified; missing cusps remain unknown. Do not infer or recalculate.",
    }
    # Keep every placement and cusp; drop whole aspect rows from the least important end.
    rows = context["aspects"]
    value = compact_json(context)
    while rows and len(value.encode('utf-8')) > CHART_CONTEXT_BUDGET:
        rows.pop()
        context["omitted"]["aspects"] += 1
        value = compact_json(context)
    if len(value.encode('utf-8')) > CHART_CONTEXT_BUDGET:
        raise HTTPException(422, "Данные карты превышают бюджет AI-контекста; требуется проверка карты.")
    return value
