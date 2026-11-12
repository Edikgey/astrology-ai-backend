"""CHAT-QUALITY-eval: the facts of the synthetic charts and the code check of answers. No network, no database.

1. charts.json (the saved rows the measurement reads) equals what the product calculator computes now, and agrees
   with the reference facts of EVAL-SET.md section 2.
2. Fact extraction from Russian answers: 30 phrases with the expected verdict.
3. Markup/filler metrics of textmetrics.
"""
import json
import os
import socket
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

with patch("dotenv.load_dotenv"), patch.dict(os.environ, {"DATABASE_URL": "sqlite://", "SECRET_KEY": "chat-eval-tests",
                                                          "OPENAI_API_KEY": "stub-not-a-key"}):
    from api.endpoints import clean_json_data
    from modules.aspects import Aspects
    from modules.ephemeris import Ephemeris
    from modules.patterns import AstrologicalPatterns

from tools.chat_eval import textmetrics
from tools.chat_eval.facts import (ChartFacts, check_answer, degree_in_sign, degree_text, extract_claims, is_russian,
                                   verify_claims)

CHARTS = json.loads((Path(__file__).resolve().parent.parent / "tools" / "chat_eval" / "charts.json").read_text(encoding="utf-8"))

# EVAL-SET.md section 2, "Опорные факты": body -> (sign, degree, minutes, house or None, retrograde)
REFERENCE = {
    "A": {"☉": ("Рыбы", 23, 5, 12, False), "☽": ("Водолей", 26, 52, 12, False), "AS": ("Овен", 11, 38, 1, False),
          "MC": ("Козерог", 3, 35, 10, False), "☿": ("Овен", 4, 18, 12, False), "♀": ("Овен", 24, 15, 1, False),
          "♂": ("Близнецы", 19, 52, 3, False), "♃": ("Лев", 3, 58, 5, True)},
    "B": {"☉": ("Дева", 5, 46, 5, False), "☽": ("Скорпион", 4, 48, 6, False), "AS": ("Близнецы", 0, 28, 1, False),
          "MC": ("Козерог", 24, 34, 10, False), "♀": ("Дева", 7, 31, 5, False), "♃": ("Овен", 29, 34, 12, True),
          "♄": ("Стрелец", 14, 37, 7, False)},
    "C": {"☉": ("Стрелец", 13, 22, 9, False), "☽": ("Лев", 12, 14, 5, False), "AS": ("Овен", 2, 29, 1, False),
          "☿": ("Стрелец", 13, 41, 9, False), "♀": ("Стрелец", 3, 45, 8, False), "♂": ("Водолей", 27, 29, 12, False),
          "♄": ("Близнецы", 11, 19, 2, True)},
    "D": {"☉": ("Близнецы", 29, 5, 12, False), "☽": ("Телец", 17, 47, 12, False), "AS": ("Рак", 4, 22, 1, False),
          "MC": ("Водолей", 14, 2, 10, False), "☿": ("Рак", 21, 1, 2, False), "♀": ("Близнецы", 11, 18, 12, False),
          "♂": ("Телец", 26, 23, 12, False), "♅": ("Скорпион", 17, 25, 6, True)},
}
# (chart, body a, body b, aspect code, orb in degrees rounded to 0.1)
REFERENCE_ASPECTS = [
    ("A", "♃", "♄", "opp", 0.3), ("A", "☿", "♃", "tri", 0.3), ("A", "MC", "☿", "sq", 0.7), ("A", "☉", "♂", "sq", 3.2),
    ("A", "☽", "♀", "sext", 2.6), ("A", "☽", "♇", "sq", 6.6),
    ("B", "☿", "♄", "sq", 0.1), ("B", "♀", "♇", "sext", 0.1), ("B", "☉", "♂", "conj", 1.4), ("B", "☽", "♃", "opp", 5.2),
    ("B", "♀", "♄", "sq", 7.1),
    ("C", "☉", "☿", "conj", 0.3), ("C", "☉", "☽", "tri", 1.1), ("C", "☉", "♄", "opp", 2.0), ("C", "☿", "♄", "opp", 2.4),
    ("D", "☽", "♅", "opp", 0.4), ("D", "♀", "♄", "sq", 2.7), ("D", "☿", "♇", "sq", 4.6),
]


def facts(key):
    return ChartFacts(CHARTS[key])


class NoNetwork:
    """Any connection attempt fails the test: the measurement facts must come from local code and saved rows."""
    def __enter__(self):
        def refuse(*args, **kwargs):
            raise AssertionError("network access attempted")
        self.patches = [patch.object(socket.socket, "connect", refuse), patch.object(socket.socket, "connect_ex", refuse)]
        for item in self.patches:
            item.start()

    def __exit__(self, *exc):
        for item in self.patches:
            item.stop()


class ChartFactsTests(unittest.TestCase):
    def test_four_charts_are_present_and_saved_with_a_calculation(self):
        self.assertEqual(sorted(CHARTS), ["A", "B", "C", "D"])
        for key, entry in CHARTS.items():
            data = entry["data"]
            self.assertEqual(data["house_system"], "Placidus", key)
            self.assertEqual(len(data["houses"]), 12, key)
            self.assertGreater(len(data["aspects_structured"]["major"]), 30, key)
            self.assertTrue(entry["chart"]["timezone"] and entry["chart"]["birth_utc"], key)

    def test_reference_placements_of_eval_set(self):
        for key, bodies in REFERENCE.items():
            chart = facts(key)
            for symbol, (sign, degrees, minutes, house, retro) in bodies.items():
                body = chart.bodies[symbol]
                self.assertEqual((body["sign"], body["deg"], body["min"], body["house"], body["retro"]),
                                 (sign, degrees, minutes, house, retro), (key, symbol))
                self.assertEqual(body["sign"], body["sign_from_longitude"], (key, symbol))

    def test_reference_aspects_and_orbs_of_eval_set(self):
        for key, first, second, code, orb in REFERENCE_ASPECTS:
            rows = [r for r in facts(key).aspects if {r["a"], r["b"]} == {first, second} and r["code"] == code]
            self.assertEqual(len(rows), 1, (key, first, second, code))
            self.assertEqual(round(rows[0]["orb_deg"], 1), orb, (key, first, second, code))

    def test_reference_cusp_patterns_and_the_thirty_degree_display_case(self):
        self.assertEqual({c["house"]: (c["sign"], c["deg"], c["min"]) for c in facts("B").cusps}[7], ("Стрелец", 0, 28))
        self.assertEqual(facts("A").patterns, [])
        types = {key: sorted(p["type"] for p in facts(key).patterns) for key in "BCD"}
        self.assertIn("Стеллиум в знаке Дева", types["B"])
        self.assertEqual(types["B"].count("Парус"), 2)
        self.assertIn("Стеллиум в знаке Стрелец", types["C"])
        self.assertIn("Мистический Прямоугольник", types["C"])
        self.assertIn("Бумеранг", types["D"])
        stellium = next(p for p in facts("B").patterns if p["type"].startswith("Стеллиум"))
        self.assertEqual(sorted(stellium["bodies"]), ["Венера", "Марс", "Меркурий", "Солнце"])
        # The old table rounds 29 deg 34 min up to 30; the measurement counts the dropped value.
        self.assertEqual(CHARTS["B"]["data"]["bodies_for_circle"]["♃"]["roundedDegree"], "30°")
        self.assertEqual((facts("B").bodies["♃"]["deg"], facts("B").bodies["♃"]["min"]), (29, 34))

    def test_no_asteroids_in_the_saved_charts(self):
        for key in CHARTS:
            labels = {b["name"] for b in facts(key).bodies.values()}
            self.assertFalse(labels & {"Хирон", "Церера", "Паллада", "Юнона", "Веста"}, key)

    def test_charts_json_equals_what_the_product_calculates_now(self):
        """Recalculate with the product calculators (JPL asteroid request off, network refused) and compare every row."""
        for key, entry in CHARTS.items():
            utc = datetime.fromisoformat(entry["chart"]["birth_utc"])
            args = (utc.year, utc.month, utc.day, utc.hour + utc.minute / 60 + utc.second / 3600,
                    entry["chart"]["lon"], entry["chart"]["lat"])
            with NoNetwork(), patch.object(Ephemeris, "_asteroid_snapshot", lambda self: {}):
                ephemeris = Ephemeris(*args)
                bodies = clean_json_data(ephemeris.get_all_bodies_with_degrees())
                aspects = Aspects(*args, ephemeris=ephemeris)
                structured = clean_json_data(aspects.get_all_aspects_structured())
                houses = clean_json_data(ephemeris.get_house_cusps())
                raw = [line.strip() for line in aspects.get_all_aspects_flat().split("\n") if line.strip()]
                patterns = clean_json_data(AstrologicalPatterns(aspects.convert_aspects_to_symbols(raw),
                                                                ephemeris.get_all_bodies()).get_patterns_structured())
            data = entry["data"]
            self.assertEqual(bodies, data["bodies_for_circle"], key)
            self.assertEqual(structured, data["aspects_structured"], key)
            self.assertEqual(houses, data["houses"], key)
            self.assertEqual(patterns, data["patterns_data"], key)

    def test_degree_in_sign_drops_minutes_and_never_rounds_into_the_next_sign(self):
        self.assertEqual(degree_in_sign(353.0846), (23, 5))
        self.assertEqual(degree_in_sign(29.5667), (29, 34))
        self.assertEqual(degree_in_sign(0.0), (0, 0))
        self.assertEqual(degree_in_sign(359.9999), (29, 59))
        self.assertEqual(degree_text(353.0846), "23°05′")

    def test_judge_facts_use_names_degrees_and_orbs_not_glyphs(self):
        packed = facts("A").for_judge()
        self.assertIn(["Луна", "Водолей", "26°52′", 12, False], packed["placements"])
        self.assertIn(["Меркурий", "трин", "Юпитер", 0.33, "major"], packed["aspects"])
        self.assertNotIn("☽", json.dumps(packed, ensure_ascii=False))


# (chart, answer text, expected [(kind, verdict), ...])
PHRASES = [
    ("A", "Ваша Луна в Водолее.", [("sign", "confirmed")]),
    ("A", "Луна находится в Овне.", [("sign", "contradicted")]),
    ("A", "Солнце в Рыбах, в двенадцатом доме.", [("sign", "confirmed"), ("house", "confirmed")]),
    ("A", "Меркурий у вас в 12-м доме.", [("house", "confirmed")]),
    ("A", "Венера в Овне в первом доме.", [("sign", "confirmed"), ("house", "confirmed")]),
    ("A", "Венера во 2 доме.", [("house", "contradicted")]),
    ("A", "Юпитер в оппозиции к Сатурну, орбис 0.3°.", [("aspect", "confirmed")]),
    ("A", "Меркурий в трине с Юпитером.", [("aspect", "confirmed")]),
    ("A", "Меркурий в квадрате с Юпитером.", [("aspect", "contradicted")]),
    ("A", "Луна в секстиле с Венерой.", [("aspect", "confirmed")]),
    ("B", "Юпитер в Овне на 29°34′, ретроградный.", [("sign", "confirmed"), ("degree", "confirmed")]),
    ("B", "Юпитер у вас на 30° Овна.", [("degree", "contradicted")]),
    ("B", "Солнце в Деве на 5°46′.", [("sign", "confirmed"), ("degree", "confirmed")]),
    ("B", "Солнце в Деве на 7°.", [("sign", "confirmed"), ("degree", "contradicted")]),
    ("A", "Если Луна в Овне, эмоции быстрые.", []),
    ("A", "Обычно Солнце в Рыбах мечтательно.", []),
    ("A", "Луна не в Овне.", []),
    ("A", "Например, Луна в Раке — это забота.", []),
    ("A", "Луна в Водолее; Солнце в Овне.", [("sign", "confirmed"), ("sign", "contradicted")]),
    ("C", "Стеллиум в Стрельце: Солнце, Меркурий, Венера. Солнце на 13°22′.", [("degree", "confirmed")]),
    ("C", "Солнце в соединении с Меркурием.", [("aspect", "confirmed")]),
    ("C", "Меркурий в оппозиции к Сатурну.", [("aspect", "confirmed")]),
    ("D", "Луна в оппозиции к Урану — ваш самый точный аспект.", [("aspect", "confirmed")]),
    ("D", "Солнце в Близнецах на 29°05′.", [("sign", "confirmed"), ("degree", "confirmed")]),
    ("D", "Асцендент в Раке на 4°22′.", [("sign", "confirmed"), ("degree", "confirmed")]),
    ("D", "Меркурий в Раке во втором доме.", [("sign", "confirmed"), ("house", "confirmed")]),
    ("D", "Марс в Тельце, Луна в Тельце.", [("sign", "confirmed"), ("sign", "confirmed")]),
    ("D", "Средина Неба в Водолее.", [("sign", "confirmed")]),
    ("A", "Солнце находится в 5 градусах от Марса.", []),
    ("A", "Люди с Луной в Овне импульсивны.", []),
    ("A", "Чёрная Луна в Овне и Луна в Водолее.", [("sign", "confirmed")]),
]


class ClaimExtractionTests(unittest.TestCase):
    def test_thirty_russian_phrases(self):
        self.assertGreaterEqual(len(PHRASES), 30)
        for key, text, expected in PHRASES:
            verified = verify_claims(extract_claims(text), facts(key))
            self.assertEqual([(c["kind"], c["verdict"]) for c in verified], expected, text)

    def test_contradiction_carries_the_sentence_and_the_expected_value(self):
        result = check_answer("Ваша Луна в Овне. Солнце в Рыбах.", facts("A"))
        self.assertEqual((result["confirmed"], result["contradicted"]), (1, 1))
        self.assertEqual(result["contradictions"][0]["expected"], "Водолей")
        self.assertEqual(result["contradictions"][0]["sentence"], "Ваша Луна в Овне.")

    def test_a_body_missing_from_the_chart_is_unverifiable_not_contradicted(self):
        chart = facts("A")
        del chart.bodies["♅"]
        verified = verify_claims(extract_claims("Уран в Скорпионе."), chart)
        self.assertEqual([c["verdict"] for c in verified], ["unverifiable"])

    def test_language_detection(self):
        self.assertTrue(is_russian("Ваша Луна в Водолее и это важно."))
        self.assertFalse(is_russian("Your Moon is in Aquarius and it matters."))

    def test_markdown_symbols_do_not_hide_a_claim(self):
        verified = verify_claims(extract_claims("**Луна в Водолее** — вот что важно.\n\n- Солнце в Рыбах"), facts("A"))
        self.assertEqual([(c["kind"], c["verdict"]) for c in verified], [("sign", "confirmed"), ("sign", "confirmed")])


class TextMetricTests(unittest.TestCase):
    def test_old_interface_visible_markup_and_unsupported_constructs(self):
        text = "## Заголовок\n\n**Жирное** и `код`.\n- пункт\n1. номер\n| a | b |\n[ссылка](http://x)\n<b>тег</b>"
        flags = textmetrics.markup_flags(text)
        self.assertEqual(flags["old_visible"], ["backtick", "bold", "bullet", "heading", "numbered", "table"])
        self.assertEqual(flags["new_unsupported"], ["html", "link", "table"])
        self.assertEqual(textmetrics.markup_flags("Просто текст. Два предложения.")["old_visible"], [])

    def test_shape_counts_words_paragraphs_and_list_items(self):
        shape = textmetrics.shape("Один абзац.\n\n- а\n- б\n\nТретий абзац здесь.")
        self.assertEqual((shape["paragraphs"], shape["list_items"], shape["words"]), (3, 2, 7))

    def test_intro_outro_chancery_emoji_and_latin_share(self):
        text = "Конечно! Данный аспект является важным. Если хотите, могу рассказать больше 😊"
        result = textmetrics.filler(text)
        self.assertTrue(result["intro"])
        self.assertTrue(result["outro"])
        self.assertEqual(result["chancery"], 2)
        self.assertEqual(result["emoji"], 1)
        self.assertEqual(textmetrics.filler("Луна в Водолее")["start2"], "луна в")

    def test_same_start_share(self):
        texts = ["Луна в Водолее.", "Луна в Овне.", "Совсем другое начало."]
        self.assertAlmostEqual(textmetrics.same_start_share(texts), 2 / 3)


if __name__ == "__main__":
    unittest.main()
