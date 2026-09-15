import copy
from datetime import datetime
from types import SimpleNamespace
import unittest
from modules.synastry import (PLANETS, MAJOR_ASPECTS, SynastryInputError, aspect_rows,
                              build_synastry_snapshot, validate_synastry_input)
from modules.cusp_math import house_for_longitude


def facts(identifier=1, offset=0):
    chart = SimpleNamespace(id=identifier, timezone="UTC", birth_utc=datetime(2000, 1, 2, 12))
    stored = SimpleNamespace(bodies_for_circle={body: {"degree": (i * 31 + offset) % 360, "house": 12,
                                                     "roundedDegree": "WRONG", "sign": "WRONG"}
                                                for i, body in enumerate(PLANETS)},
                             houses=[{"symbol": str(i + 1), "degree": (i * 30 + offset) % 360} for i in range(12)],
                             house_system="Placidus")
    stored.bodies_for_circle.update({"AS": {"degree": offset % 360}, "MC": {"degree": (offset + 270) % 360}})
    return chart, stored


class SynastryTests(unittest.TestCase):
    def test_exact_major_aspects_and_same_body_names(self):
        for symbol, angle in MAJOR_ASPECTS:
            with self.subTest(symbol=symbol):
                row, = aspect_rows({"☉": 0}, {"☉": angle})
                self.assertEqual((row["participant_a"], row["participant_b"], row["aspect"]), ("A", "B", symbol))
                self.assertEqual(row["orb_deg"], 0)

    def test_wraparound_and_full_precision_orb_boundaries(self):
        row, = aspect_rows({"☉": 359}, {"☽": 1})
        self.assertEqual(row["separation_deg"], 2)
        for angle in (7.999999, 8):
            self.assertEqual(len(aspect_rows({"☉": 0}, {"☽": angle})), 1)
        self.assertEqual(aspect_rows({"☉": 0}, {"☽": 8.000001}), [])
        self.assertEqual(aspect_rows({"☉": 0}, {"☽": 68.000001}), [])

    def test_both_overlay_directions_ignore_own_house_and_display_labels(self):
        result = build_synastry_snapshot(*facts(), *facts(2, 30))
        ab, ba = result["house_overlays"]
        self.assertEqual((ab["planet_participant"], ab["house_participant"]), ("A", "B"))
        self.assertEqual(ab["placements"][0]["house"], 12)
        self.assertEqual(ba["placements"][0]["house"], 2)

    def test_cusp_boundary_and_wraparound(self):
        cusps = [(350 + i * 30) % 360 for i in range(12)]
        for i, cusp in enumerate(cusps):
            self.assertEqual(house_for_longitude(cusp, cusps), i + 1)
            self.assertEqual(house_for_longitude(cusp + 1e-6, cusps), i + 1)
            self.assertEqual(house_for_longitude(cusp - 1e-6, cusps), (i - 1) % 12 + 1)
        self.assertEqual(house_for_longitude(0, cusps), 1)

    def test_invalid_or_missing_cusps_disable_only_target_direction(self):
        for houses in (None, [], [{"symbol": str(i + 1), "degree": 0} for i in range(12)],
                       [{"symbol": str(i + 1), "degree": (330 - i * 30)} for i in range(12)],
                       [{"symbol": "1", "degree": i * 30} for i in range(12)]):
            with self.subTest(houses=houses):
                b, saved_b = facts(2, 30)
                saved_b.houses = houses
                result = build_synastry_snapshot(*facts(), b, saved_b)
                self.assertFalse(result["house_overlays"][0]["available"])
                self.assertEqual(result["house_overlays"][0]["placements"], [])
                self.assertTrue(result["house_overlays"][1]["available"])

    def test_unknown_house_system_does_not_infer_placidus(self):
        b, saved = facts(2)
        saved.house_system = None
        result = build_synastry_snapshot(*facts(), b, saved)
        self.assertEqual(result["house_overlays"][0]["reason"], "unsupported_or_missing_house_system")

    def test_missing_angles_are_not_derived_from_cusps(self):
        b, saved = facts(2)
        del saved.bodies_for_circle["AS"]
        saved.bodies_for_circle["MC"]["degree"] = float("nan")
        result = build_synastry_snapshot(*facts(), b, saved)
        self.assertEqual(result["angle_availability"]["B"], {"AS": False, "MC": False})
        self.assertFalse(any(r["participant_b"] == "B" and r["body_b"] in ("AS", "MC") for r in result["aspects"]))
        self.assertTrue(any(r["participant_b"] == "A" and r["body_b"] in ("AS", "MC") for r in result["aspects"]))

    def test_legacy_and_invalid_timezone_rejected(self):
        for field, value in (("birth_utc", None), ("timezone", None), ("timezone", "Unknown/Invalid")):
            chart, stored = facts()
            setattr(chart, field, value)
            with self.assertRaises(SynastryInputError):
                validate_synastry_input(chart, stored, "A")

    def test_missing_or_invalid_planet_longitude_rejected(self):
        for value in (None, "12.5", True, float("nan"), float("inf"), -1, 360):
            chart, stored = facts()
            stored.bodies_for_circle["☉"]["degree"] = value
            with self.assertRaises(SynastryInputError):
                validate_synastry_input(chart, stored, "A")
        chart, stored = facts()
        del stored.bodies_for_circle["☉"]
        with self.assertRaises(SynastryInputError):
            validate_synastry_input(chart, stored, "A")

    def test_snapshot_deterministic_non_mutating_and_ignores_excluded_bodies(self):
        a, da = facts()
        b, db = facts(2, 30)
        before = copy.deepcopy(da.__dict__)
        first = build_synastry_snapshot(a, da, b, db)
        self.assertEqual(first, build_synastry_snapshot(a, da, b, db))
        self.assertEqual(da.__dict__, before)
        da.bodies_for_circle["Vx"] = {"degree": 100}
        self.assertEqual(first, build_synastry_snapshot(a, da, b, db))
        self.assertEqual(first["ruleset_version"], "synastry-major-8-v1")

    def test_cusp_extraction_matches_original_natal_algorithm(self):
        # Characterization of the pre-extraction interval algorithm, including its tolerance.
        def original(degree, cusps):
            degree %= 360
            for i, cusp in enumerate(cusps):
                if abs((degree - cusp + 180) % 360 - 180) < 1e-10:
                    return i + 1
            for i in range(12):
                start, end = cusps[i], cusps[(i + 1) % 12]
                if start > end:
                    if start <= degree or degree < end:
                        return i + 1
                elif start <= degree < end:
                    return i + 1
            return 12
        for offset in (0, 20.125, 359.999):
            cusps = [(offset + i * 30) % 360 for i in range(12)]
            for value in [i * 0.37 for i in range(-50, 1000)] + [c + d for c in cusps for d in (-1e-11, 0, 1e-11)]:
                self.assertEqual(house_for_longitude(value, cusps), original(value, cusps))
