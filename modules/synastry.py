"""Versioned synastry from stored ChartData only. No DB, provider or ephemeris calls."""
from datetime import datetime
from hashlib import sha256
import json
import math
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from modules.cusp_math import house_for_longitude

RULESET_VERSION = "synastry-major-8-v1"
SNAPSHOT_VERSION = 1
PLANETS = ("☉", "☽", "☿", "♀", "♂", "♃", "♄", "♅", "♆", "♇")
ANGLES = ("AS", "MC")
MAJOR_ASPECTS = (("☌", 0), ("⚹", 60), ("□", 90), ("△", 120), ("☍", 180))
ORB_LIMIT = 8.0  # Product ruleset, not a universal astrological constant.


class SynastryInputError(ValueError):
    def __init__(self, participant, reason):
        self.participant, self.reason = participant, reason
        super().__init__(reason)


def valid_longitude(value):
    return type(value) in (int, float) and math.isfinite(value) and 0 <= value < 360


def stored_cusps(stored):
    if stored.house_system != "Placidus":
        return None, "unsupported_or_missing_house_system"
    rows = stored.houses
    if not isinstance(rows, list) or len(rows) != 12:
        return None, "missing_or_invalid_cusps"
    by_number = {}
    for row in rows:
        if not isinstance(row, dict) or row.get("symbol") not in tuple(str(i) for i in range(1, 13)):
            return None, "missing_or_invalid_cusps"
        number = row["symbol"]
        if number in by_number or not valid_longitude(row.get("degree")):
            return None, "missing_or_invalid_cusps"
        by_number[number] = row["degree"]
    cusps = [by_number[str(i)] for i in range(1, 13)]
    spans = [(cusps[(i + 1) % 12] - cusp) % 360 for i, cusp in enumerate(cusps)]
    if any(span <= 1e-10 for span in spans) or not math.isclose(sum(spans), 360, abs_tol=1e-8, rel_tol=0):
        return None, "missing_or_invalid_cusps"
    return cusps, None


def validate_synastry_input(chart, stored, participant):
    if not isinstance(chart.birth_utc, datetime) or not isinstance(chart.timezone, str) or not chart.timezone:
        raise SynastryInputError(participant, "legacy_unverified")
    try:
        ZoneInfo(chart.timezone)
    except (ZoneInfoNotFoundError, ValueError):
        raise SynastryInputError(participant, "legacy_unverified") from None
    bodies = stored.bodies_for_circle if stored is not None else None
    if not isinstance(bodies, dict):
        raise SynastryInputError(participant, "missing_saved_positions")
    positions = {}
    for body in PLANETS:
        row = bodies.get(body)
        if not isinstance(row, dict) or not valid_longitude(row.get("degree")):
            raise SynastryInputError(participant, "missing_or_invalid_planet_longitude")
        positions[body] = row["degree"]
    angles = {body: bodies[body]["degree"] for body in ANGLES
              if isinstance(bodies.get(body), dict) and valid_longitude(bodies[body].get("degree"))}
    cusps, reason = stored_cusps(stored)
    return {"chart_id": chart.id, "positions": positions, "angles": angles,
            "cusps": cusps, "house_system": "Placidus" if cusps is not None else None,
            "overlays_unavailable_reason": reason,
            "time_provenance": {"timezone": chart.timezone, "birth_utc": chart.birth_utc.isoformat()}}


def aspect_rows(positions_a, positions_b, participant_a="A", participant_b="B"):
    rows = []
    for body_a, longitude_a in positions_a.items():
        for body_b, longitude_b in positions_b.items():
            difference = abs(longitude_a - longitude_b)
            separation = min(difference, 360 - difference)
            for symbol, angle in MAJOR_ASPECTS:
                orb = abs(separation - angle)
                if orb <= ORB_LIMIT:
                    rows.append({"participant_a": participant_a, "body_a": body_a,
                                 "participant_b": participant_b, "body_b": body_b,
                                 "aspect": symbol, "aspect_angle": angle,
                                 "separation_deg": separation, "orb_deg": orb, "orb_limit_deg": ORB_LIMIT})
    return rows


def overlay_rows(source, target, source_person, target_person):
    available = target["cusps"] is not None
    return {"planet_participant": source_person, "house_participant": target_person,
            "available": available, "reason": target["overlays_unavailable_reason"],
            "placements": [{"body": body, "house": house_for_longitude(longitude, target["cusps"])}
                           for body, longitude in source["positions"].items()] if available else []}


def build_synastry_snapshot(chart_a, data_a, chart_b, data_b):
    a = validate_synastry_input(chart_a, data_a, "A")
    b = validate_synastry_input(chart_b, data_b, "B")
    # Each saved angle is independently available; never derive one from a cusp.
    aspects = aspect_rows(a["positions"], b["positions"])
    aspects += aspect_rows(a["positions"], b["angles"])
    aspects += aspect_rows(b["positions"], a["angles"], "B", "A")
    inputs = {"A": a, "B": b}
    fingerprint = sha256(json.dumps(inputs, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()
    return {"schema_version": SNAPSHOT_VERSION, "ruleset_version": RULESET_VERSION,
            "orb_limit_deg": ORB_LIMIT, "inputs": inputs, "input_fingerprint": fingerprint,
            "aspects": aspects,
            "angle_availability": {p: {angle: angle in facts["angles"] for angle in ANGLES}
                                   for p, facts in inputs.items()},
            "house_overlays": [overlay_rows(a, b, "A", "B"), overlay_rows(b, a, "B", "A")]}
