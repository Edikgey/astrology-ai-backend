"""Compact chart facts from existing calculator outputs; no interpretive inventions."""
import json
from fastapi import HTTPException
from database.queries import NatalChart, ChartData


def compact_json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


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
    aspects, patterns = stored.aspects_for_circle, stored.patterns_data
    placements = []
    for symbol, body in (bodies or {}).items():
        placements.append([symbol, body.get("label"), body.get("sign"), body.get("degree"),
                           body.get("house"), body.get("retrograde")])
    # Each aspect/pattern appears once, not once per participating planet.
    aspect_rows = sorted({(min(a['from_body'], a['to_body']), max(a['from_body'], a['to_body']), a['aspect'])
                          for a in (aspects or [])})
    pattern_rows = []
    for pattern in patterns or []:
        row = [pattern['type'], sorted({b['symbol'] for b in pattern['bodies']})]
        if row not in pattern_rows:
            pattern_rows.append(row)
    context = {
        "birth": {"date": [chart.year, chart.month, chart.day], "local_hour": chart.hour,
                  "timezone": chart.timezone,
                  "utc": chart.birth_utc.isoformat() + "Z" if chart.birth_utc else None,
                  "location": [chart.city, chart.region, chart.country], "lon": chart.lon, "lat": chart.lat},
        "placement_columns": ["body", "name", "sign", "ecliptic_longitude_deg", "house", "retrograde"],
        "placements": placements,
        "core_bodies": [s for s in ("☉", "☽", "AS") if s in (bodies or {})],
        "houses": {"system": stored.house_system,
                   "cusp_longitudes_deg": stored.houses},
        "aspect_columns": ["body_a", "body_b", "aspect_symbol"], "aspects": aspect_rows,
        "pattern_columns": ["type", "bodies"], "patterns": pattern_rows,
        "limitations": "Only natal calculations; no transits." if chart.birth_utc and chart.timezone else
            "Legacy chart: birth timezone/minutes unverified; missing cusps remain unknown. Do not infer or recalculate.",
    }
    value = compact_json(context)
    if len(value.encode('utf-8')) > 24000:
        raise HTTPException(422, "Данные карты превышают бюджет AI-контекста; требуется проверка карты.")
    return value
