"""CHAT-CLAUDE step 3, E-9 and E-10: the new questions and conversations, the owner's blind page G* against H* (driven
in a real DOM as a user would), the summary line the report parses, and the report assembled from a synthetic run.
Local only: jsdom from the frontend's node_modules runs the page script; nothing touches a network.
"""
import json
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

from chat_eval_fake_server import FakeOpenAI
from test_chat_eval_budget import REPO, run_cli
from tools.chat_eval import budget, cli, common, facts as facts_mod, judge, page, questions as Q, report

CHARTS = json.loads((REPO / "tools" / "chat_eval" / "charts.json").read_text(encoding="utf-8"))
NEW_IDS = ["N17", "N18", "R06", "S1-A", "S1-B", "S1-C", "S1-D"]


def lines(path):
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]


# ------------------------------------------------------------------------------------------------------- E-9
class NewQuestionTests(unittest.TestCase):
    def test_the_texts_are_the_designed_ones(self):
        self.assertEqual(Q.question_text("N17"), "Что в моей карте самое трудное? Скажите честно, без утешений.")
        self.assertEqual(Q.question_text("N18"), "Друзья говорят, что я слишком критична к людям. Это видно по карте? Только честно.")
        self.assertEqual(Q.question_text("R06"), "Хорошо, без процентов. Что в наших отношениях будет самым трудным и что нам с этим делать?")
        for q in ("S1-A", "S1-B", "S1-C", "S1-D"):
            self.assertEqual(Q.question_text(q), "Опишите мой характер в трёх-четырёх фразах: что во мне главное?")

    def test_28_evaluated_questions_and_x01_stays_outside(self):
        self.assertEqual(len(Q.QUESTIONS), 28)
        self.assertEqual(len(Q.EVALUATED_IDS), 28)
        self.assertNotIn("X01", Q.EVALUATED_IDS)
        for q in NEW_IDS:
            self.assertIn(q, Q.QUESTIONS)
            self.assertIn(q, Q.EVALUATED_IDS)
        self.assertEqual(sum(len(c[3]) for c in Q.CONVERSATIONS), 29)

    def test_each_new_question_is_in_the_designed_conversation(self):
        by = {c[0]: c for c in Q.CONVERSATIONS}
        self.assertEqual(by["A3"][1:], ("natal", ("A",), ["N17"]))
        self.assertEqual(by["B2"][1:], ("natal", ("B",), ["N18"]))
        self.assertEqual(by["P2"][3], ["R04", "R05", "R06"])                    # R06 comes third, after R05, in the same conversation
        for conv, chart in (("SA", "A"), ("SB", "B"), ("SC", "C"), ("SD", "D")):
            self.assertEqual(by[conv][1:], ("natal", (chart,), ["S1-" + chart]))
        for old in ("A", "B", "C", "D", "A2", "P1"):
            self.assertIn(old, by)
        self.assertEqual(Q.CHART_COPIES, {"A2": "A", "A3": "A", "B2": "B", "SA": "A", "SB": "B", "SC": "C", "SD": "D"})

    def test_subsets_of_the_design(self):
        self.assertEqual(list(Q.S10_IDS), ["N01", "N03", "N05", "N08", "N11", "N13", "N17", "N18", "R02", "R06"])
        self.assertEqual(list(Q.CLAUDE_PAGE_IDS), ["N01", "N03", "N05", "N08", "N11", "N12", "N14", "N17", "N18", "R02", "R06", "S1-B"])
        self.assertEqual(len(Q.CLAUDE_PAGE_IDS), 12)
        for q in list(Q.S10_IDS) + list(Q.CLAUDE_PAGE_IDS):
            self.assertIn(q, Q.QUESTIONS)
        self.assertEqual(len(Q.OWNER_PAGE_IDS), 12)                             # the older page is untouched

    def test_the_judge_notes_carry_the_facts_to_check(self):
        notes = {q: Q.QUESTIONS[q][2] for q in NEW_IDS}
        self.assertIn("0.3", notes["N17"])
        self.assertIn("Saturn", notes["N17"])
        self.assertIn("12", notes["N17"])
        self.assertIn("Virgo", notes["N18"])
        self.assertIn("0.1", notes["N18"])
        self.assertIn("0.03", notes["R06"])
        for q in ("S1-A", "S1-B", "S1-C", "S1-D"):
            self.assertIn("this chart", notes[q])
        self.assertEqual({Q.QUESTIONS[q][0] for q in ("N17", "N18", "R06")}, {"balance"})
        self.assertEqual({Q.QUESTIONS[q][0] for q in ("S1-A", "S1-B", "S1-C", "S1-D")}, {"character"})

    def test_the_product_transit_filter_lets_every_new_question_through(self):
        from modules.ai_transit_guard import requires_transit_data
        for q in NEW_IDS:
            self.assertFalse(requires_transit_data(Q.question_text(q)), q)

    def test_the_judge_can_build_a_request_for_each_new_question(self):
        facts = {"natal": {k: facts_mod.ChartFacts(v).for_judge() for k, v in CHARTS.items()}, "pairs": {}}
        for q in ("N17", "N18", "S1-A", "S1-D"):
            record = {"q": q, "run": 1, "question": Q.question_text(q), "subject": "natal", "chart": q[-1] if q.startswith("S1") else {"N17": "A", "N18": "B"}[q],
                      "conv": "x", "seeded_history": False, "history": [], "config": "G3", "response": "ответ"}
            messages = judge.build_messages(record, dict(record, config="H3"), facts)
            self.assertEqual(json.loads(messages[1]["content"])["question"], Q.question_text(q))


class NewQuestionsThroughTheProductPathTests(unittest.TestCase):
    """The worker runs the new conversations the way the product route does; copies of a chart have their own history."""

    def run_worker(self, only, runs=1):
        with FakeOpenAI() as server, tempfile.TemporaryDirectory() as tmp:
            result = run_cli("run", "--configs", "C0", "--runs", str(runs), "--only", only, "--base-repo", str(REPO),
                             "--candidate-repo", str(REPO), "--run-dir", str(Path(tmp) / "run"), "--base-url", server.url)
            made = server.count("generation")
            answers = lines(Path(tmp) / "run" / "answers.jsonl") if (Path(tmp) / "run" / "answers.jsonl").is_file() else []
        self.assertEqual(result.returncode, 0, result.stdout[-800:])
        return answers, made

    def test_chart_copies_and_the_third_pair_question(self):
        answers, made = self.run_worker("N17,N18,S1-A,S1-B,S1-C,S1-D,R04,R05,R06")
        by = {a["q"]: a for a in answers}
        self.assertEqual(sorted(by), sorted(["N17", "N18", "S1-A", "S1-B", "S1-C", "S1-D", "R04", "R05", "R06"]))
        self.assertEqual(made, 9)
        self.assertEqual({q: a["outcome"] for q, a in by.items()}, {q: "ok" for q in by})
        self.assertEqual((by["N17"]["conv"], by["N17"]["chart"], by["N17"]["history"]), ("A3", "A", []))
        self.assertEqual((by["N18"]["conv"], by["N18"]["chart"], by["N18"]["history"]), ("B2", "B", []))
        for q, chart in (("S1-A", "A"), ("S1-B", "B"), ("S1-C", "C"), ("S1-D", "D")):
            self.assertEqual((by[q]["conv"], by[q]["chart"], by[q]["history"]), ("S" + chart, chart, []))
        self.assertEqual([h["q"] for h in by["R06"]["history"]], [Q.question_text("R04"), Q.question_text("R05")])
        self.assertEqual((by["R06"]["subject"], by["R06"]["conv"]), ("pair", "P2"))


# ------------------------------------------------------------------------------------------------------- E-10
def conv_of(q):
    for conv, subject, chart_key, qids in Q.CONVERSATIONS:
        if q in qids:
            return conv, subject, "+".join(chart_key)
    raise KeyError(q)


def natal_and_pairs():
    natal = {k: facts_mod.ChartFacts(v).for_judge() for k, v in CHARTS.items()}
    return {"natal": natal, "pairs": {"P1": {"people": {"A": {"label": "Маша", **natal["A"]}, "B": {"label": "Дима", **natal["B"]}}},
                                      "P2": {"people": {"A": {"label": "Оля", **natal["C"]}, "B": {"label": "Артём", **natal["D"]}}}}}


def answer(config, q, text, **extra):
    conv, subject, chart = conv_of(q)
    return {"config": config, "q": q, "run": 1, "outcome": "ok", "response": text, "conv": conv, "subject": subject, "chart": chart,
            "question": Q.question_text(q), "history": [], "seeded_history": False, "cost_usd": 0.002, "ttfb_ms": 1500,
            "duration_ms": 6000, "follow_up_count": 3, "summary_calls": 0, "input_tokens": 9000, "output_tokens": 500, **extra}


def page_answers(g="G3", h="H3"):
    rows = []
    for q in Q.CLAUDE_PAGE_IDS:
        rows += [answer(g, q, "Первый вариант ответа на %s." % q), answer(h, q, "Второй вариант ответа на %s." % q)]
    return rows


def jsdom_path():
    return common.find_root() / "astrology-frontend-main" / "node_modules" / "jsdom"


DRIVER = """
const { JSDOM } = require(process.argv[2]);
const fs = require('fs');
const dom = new JSDOM(fs.readFileSync(process.argv[3], 'utf8'), { runScripts: 'dangerously' });
const d = dom.window.document;
for (const a of JSON.parse(fs.readFileSync(process.argv[4], 'utf8'))) {
  if (a.radio) { const r = d.querySelector('input[name="' + a.radio + '"][value="' + a.value + '"]'); if (!r) { console.error('no radio ' + a.radio); process.exit(3); } r.click(); }
  if (a.note) { d.querySelector('.card[data-id="' + a.id + '"] textarea').value = a.note; }
}
d.getElementById('copy').click();
process.stdout.write(d.getElementById('out').value);
"""


def user_session(html_path, actions):
    """What the owner does in the browser: tick radios, type a comment, press the copy button, read the text it produces."""
    assert jsdom_path().is_dir(), "jsdom is needed to drive the page: %s" % jsdom_path()
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "driver.js").write_text(DRIVER, encoding="utf-8")
        (Path(tmp) / "actions.json").write_text(json.dumps(actions), encoding="utf-8")
        result = subprocess.run(["node", str(Path(tmp) / "driver.js"), str(jsdom_path()), str(html_path), str(Path(tmp) / "actions.json")],
                                capture_output=True, text=True, encoding="utf-8", timeout=120)
    assert result.returncode == 0, result.stderr[-500:]
    return result.stdout


class ClaudePageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.facts = natal_and_pairs()

    def build(self, seed="seed", answers=None, g="G3", h="H3"):
        written = report.build_claude_pages(self.dir, answers or page_answers(g, h), self.facts, seed, g, h)
        return written, (self.dir / "compare-claude.html").read_text(encoding="utf-8"), json.loads((self.dir / "key.json").read_text(encoding="utf-8"))

    def test_twelve_cards_in_the_designed_order_with_two_answers_each(self):
        written, html, key = self.build()
        self.assertEqual(written, ["compare-claude.html"])
        self.assertEqual(re.findall(r'<article class="card" data-id="([^"]+)"', html), list(Q.CLAUDE_PAGE_IDS))
        self.assertEqual(html.count('class="answer"'), 24)
        self.assertEqual(sorted(key["pages"]["compare-claude.html"]), sorted(Q.CLAUDE_PAGE_IDS))

    def test_the_page_never_names_a_model_a_configuration_a_commit_or_an_outside_address(self):
        _, html, _ = self.build()
        visible = re.sub(r"<style.*?</style>|<script.*?</script>|<[^>]+>", " ", html, flags=re.S)
        for name in ("G3", "H3", "G2", "H2", "G3x", "H3x"):
            self.assertNotIn(name, visible, name)
        lowered = html.lower()
        for secret in ("gpt", "claude", "haiku", "luna", "opus", "sonnet", "anthropic", "openai", "sha", "candidate", "compare-claude",
                       "http://", "https://", "src=", "href=", "<link", "@import", "url("):
            self.assertNotIn(secret, lowered, secret)
        self.assertEqual(html.count("<script>"), 1)
        self.assertIn('name="viewport"', html)
        self.assertIn("@media (max-width:700px)", html)

    def test_every_card_asks_for_a_pick_and_optionally_which_is_deeper_and_more_honest(self):
        _, html, _ = self.build()
        for q in Q.CLAUDE_PAGE_IDS:
            for prefix in ("v", "d", "h"):
                for value in ("1", "2", "="):
                    self.assertIn('name="%s-%s" value="%s"' % (prefix, q, value), html)
        self.assertIn("Глубже", html)
        self.assertIn("Честнее", html)
        self.assertEqual(html.count("<textarea"), 12 + 1)                      # a comment per card and the output field
        self.assertIn("Скопировать итог", html)

    def test_the_shuffle_is_seeded_mixes_the_sides_and_the_key_is_separate(self):
        _, html, key = self.build("seed")
        sides = key["pages"]["compare-claude.html"]
        self.assertEqual({s["1"] for s in sides.values()}, {"G3", "H3"})
        for card in sides.values():
            self.assertEqual(sorted(card.values()), ["G3", "H3"])
        self.assertEqual(key["claude_pair"], {"g": "G3", "h": "H3"})
        self.assertEqual(key["seed"], "seed")
        _, again, key_again = self.build("seed")
        self.assertEqual(html, again)
        self.assertEqual(key["pages"], key_again["pages"])
        _, other, key_other = self.build("another seed")
        self.assertNotEqual(key["pages"]["compare-claude.html"], key_other["pages"]["compare-claude.html"])
        for word in ("claude_pair", "seed", "pages"):
            self.assertNotIn(word, html)

    def test_an_existing_key_file_keeps_its_other_pages(self):
        (self.dir / "key.json").write_text(json.dumps({"seed": "old", "pages": {"compare.html": {"N01": {"1": "C0", "2": "C1"}}}}), encoding="utf-8")
        self.build("new")
        key = json.loads((self.dir / "key.json").read_text(encoding="utf-8"))
        self.assertEqual(sorted(key["pages"]), ["compare-claude.html", "compare.html"])

    def test_a_missing_answer_drops_its_card_and_without_both_configurations_no_page_is_built(self):
        answers = [a for a in page_answers() if not (a["q"] == "N01" and a["config"] == "H3")]
        _, html, _ = self.build(answers=answers)
        self.assertNotIn('data-id="N01"', html)
        self.assertEqual(report.build_claude_pages(self.dir / "empty", [a for a in page_answers() if a["config"] == "G3"], self.facts, "s", "G3", "H3"), [])

    def test_answers_are_drawn_by_the_same_narrow_markup_and_html_in_an_answer_is_text(self):
        answers = page_answers()
        answers[0]["response"] = "**Жирное** <script>alert(1)</script>\n\n- пункт"
        _, html, _ = self.build(answers=answers)
        self.assertEqual(html.count("<script>"), 1)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", html)

    def test_the_owner_picks_in_the_page_and_the_summary_line_is_read_back_into_models(self):
        _, html, key = self.build()
        sides = key["pages"]["compare-claude.html"]
        picks = {"N01": "1", "N03": "2", "N05": "=", "N17": "1"}
        actions = [{"radio": "v-%s" % q, "value": value} for q, value in picks.items()]
        actions += [{"radio": "d-N01", "value": "2"}, {"radio": "h-N17", "value": "="}, {"radio": "h-N03", "value": "1"},
                    {"id": "N03", "note": "Второй живее"}]
        summary = user_session(self.dir / "compare-claude.html", actions)
        self.assertRegex(summary.splitlines()[0], r"^N01:1 N03:2 N05:= N08:\? ")
        self.assertIn("S1-B:?", summary.splitlines()[0])
        result = report.parse_owner_summary(summary, key)
        expected = {q: sides[q][value] if value in "12" else "tie" for q, value in picks.items()}
        self.assertEqual({q: result["votes"][q] for q in picks}, expected)
        self.assertIsNone(result["votes"]["N08"])
        self.assertEqual(result["deeper"], {"N01": sides["N01"]["2"]})
        self.assertEqual(result["honest"], {"N17": "tie", "N03": sides["N03"]["1"]})
        self.assertEqual(result["comments"], {"N03": "Второй живее"})
        tally = result["tally"]
        self.assertEqual(sum(tally.values()), 12)
        self.assertEqual(tally["none"], 8)
        self.assertEqual(tally["tie"], 1)
        self.assertEqual(tally["G3"] + tally["H3"], 3)

    def test_the_owner_who_picks_nothing_still_gets_a_well_formed_line(self):
        _, html, key = self.build()
        summary = user_session(self.dir / "compare-claude.html", [])
        self.assertEqual(len(summary.splitlines()), 1)
        result = report.parse_owner_summary(summary, key)
        self.assertEqual(result["tally"]["none"], 12)
        self.assertEqual((result["deeper"], result["honest"], result["comments"]), ({}, {}, {}))

    def test_the_reader_is_tolerant_and_never_trusts_the_line(self):
        _, _, key = self.build()
        sides = key["pages"]["compare-claude.html"]
        text = "  N01:1   N03:2 N99:1 N05:3 N08:\nглубже: N01:2 N77:1\nчестнее:N03:=\nN01: хорошо\nN99: чужое\n"
        result = report.parse_owner_summary(text, key)
        self.assertEqual(result["votes"]["N01"], sides["N01"]["1"])
        self.assertEqual(result["votes"]["N03"], sides["N03"]["2"])
        self.assertIsNone(result["votes"]["N05"])                               # "3" is not a choice
        self.assertEqual(result["unknown"], ["N77", "N99"])
        self.assertEqual(result["deeper"], {"N01": sides["N01"]["2"]})
        self.assertEqual(result["honest"], {"N03": "tie"})
        self.assertEqual(result["comments"], {"N01": "хорошо"})
        for junk in ("", "   \n", "привет", "N01"):
            self.assertEqual(sum(report.parse_owner_summary(junk, key)["tally"].values()), 12)

    def test_the_older_owner_page_is_unchanged(self):
        card = {"id": "N01", "chart_label": "Карта", "question": "Вопрос?", "answers": [{"response": "а", "history": []}, {"response": "б", "history": []}]}
        old = page.build_page([card], "Заголовок")
        self.assertNotIn('name="d-N01"', old)
        self.assertEqual(old.count("<textarea"), 2)


# ------------------------------------------------------------------------------------------------------- the report
CONFIGS = ["C0", "G2", "G3", "G3x", "H2", "H3", "H3x"]


def sign_in(rows, name, q, left, right, outcome, depth=(4.0, 4.0), balance=(4.0, 4.0), boundary=("n/a", "n/a")):
    def scores(i):
        return {c: {"depth": depth[i], "balance": balance[i]}.get(c, 4) for c in judge.CRITERIA}

    def one(order, winner):
        return {"judge": name, "q": q, "run": 1, "left": left, "right": right, "order": order, "outcome": "ok", "winner": winner,
                "reason": "r", "scores": {left: scores(0), right: scores(1)}, "fact_errors": {left: [], right: []},
                "boundary_kept": {left: boundary[0], right: boundary[1]}}
    winner = {"left": left, "right": right, "tie": "tie"}.get(outcome)
    rows += [one("XY", winner), one("YX", winner)]


def fabricate(run_dir, same_template_for_g=True, verdicts=True):
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    answers = []
    for config in CONFIGS:
        for q in Q.EVALUATED_IDS:
            if config.startswith("H"):
                text = "Свой ответ %s номер %d. %s" % (config, Q.EVALUATED_IDS.index(q), "Вам стоит сделать шаг. " if Q.EVALUATED_IDS.index(q) % 7 else "")
                if q.startswith("S1-"):
                    text = {"S1-A": "альфа бета гамма дельта", "S1-B": "эпсилон дзета тэта йота", "S1-C": "каппа лямбда сигма омега",
                            "S1-D": "тау фи хи пси"}[q]
            else:
                text = "Вы чувствительны и цените гармонию." if q.startswith("S1-") and same_template_for_g else "Это общий ответ про ваши черты."
            cost = {"C0": 0.001, "G2": 0.002, "G3": 0.002, "G3x": 0.003, "H2": 0.0013, "H3": 0.0013, "H3x": 0.0018}[config]
            answers.append(answer(config, q, text, cost_usd=cost))
    (run_dir / "answers.jsonl").write_text("".join(json.dumps(a, ensure_ascii=False) + "\n" for a in answers), encoding="utf-8")
    (run_dir / "facts.json").write_text(json.dumps(natal_and_pairs(), ensure_ascii=False), encoding="utf-8")
    codes = {"C0": "base", "G2": "v2", "G3": "v3", "G3x": "v3", "H2": "v2", "H3": "v3", "H3x": "v3"}
    for config in CONFIGS:
        meta = {"config": config, "code": codes[config], "model": common.CONFIGS[config]["model"], "effort": common.CONFIGS[config]["effort"],
                "repo": "r", "sha": ("%040d" % (CONFIGS.index(config) + 1)), "dirty": False, "tool_sha": "t" * 40, "mode": "dry", "answers": 28}
        (run_dir / ("meta-%s.json" % config)).write_text(json.dumps(meta), encoding="utf-8")
    (run_dir / "calls.jsonl").write_text("", encoding="utf-8")
    if not verdicts:
        return
    rows = []
    ids = list(Q.EVALUATED_IDS)
    for i, q in enumerate(ids):                                                              # J1: H3 clear winner, deeper
        outcome = "right" if i < 20 else "left" if i < 24 else "tie"
        boundary = ("yes", "yes") if q in Q.BOUNDARY_IDS else ("n/a", "n/a")
        for name in ("openai", "claude"):
            sign_in(rows, name, q, "G3", "H3", outcome, depth=(4.0, 4.5), balance=(3.8, 4.3), boundary=boundary)
    for left, right in (("G2", "G3"), ("H2", "H3")):                                          # J2: the new prompt wins
        for i, q in enumerate(ids):
            boundary = ("yes", "yes") if q in Q.BOUNDARY_IDS else ("n/a", "n/a")
            sign_in(rows, "openai", q, left, right, "right" if i < 18 else "left" if i < 22 else "tie", depth=(3.5, 4.0), balance=(3.5, 4.0), boundary=boundary)
    for left, right in (("G3", "G3x"), ("H3", "H3x")):                                        # J3: the costly effort does not win
        for q in Q.S10_IDS:
            sign_in(rows, "claude", q, left, right, "tie")
    (run_dir / "judgments.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    attribution_rows = []
    for config, judge_name, correct in (("C0", "claude", 1), ("G2", "claude", 2), ("G3", "claude", 3), ("H2", "openai", 2), ("H3", "openai", 4)):
        for i in range(4):
            attribution_rows.append({"config": config, "judge": judge_name, "pair": "AD", "order": "XY", "truth": "1",
                                     "verdict": "1" if i < correct else "2", "outcome": "ok"})
    (run_dir / "attribution.jsonl").write_text("".join(json.dumps(r) + "\n" for r in attribution_rows), encoding="utf-8")
    (run_dir / "controls.json").write_text(json.dumps({"openai": {"passed": True, "pairs_ok": 6}, "claude": {"passed": True, "pairs_ok": 6}}), encoding="utf-8")
    ledger = [{"ev": "reserve", "id": "a1", "kind": "generation", "model": "gpt-6-luna", "worst_usd": 1.3, "provider": "openai"},
              {"ev": "settle", "id": "a1", "cost_usd": 1.2, "basis": "actual", "provider": "openai"},
              {"ev": "reserve", "id": "b1", "kind": "judge", "model": "claude-opus-5-5", "worst_usd": 0.9, "provider": "anthropic"},
              {"ev": "settle", "id": "b1", "cost_usd": 0.8, "basis": "actual", "provider": "anthropic"}]
    (run_dir / "spend.jsonl").write_text("".join(json.dumps(r) + "\n" for r in ledger), encoding="utf-8")


class ClaudeReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name) / "run"
        fabricate(self.dir)

    def report(self, *extra):
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()):
            code = cli.main(["report", "--claude", "--run-dir", str(self.dir), "--seed", "s", *extra])
        self.assertEqual(code, 0)
        return json.loads((self.dir / "metrics-claude.json").read_text(encoding="utf-8"))

    def test_the_files_the_owner_and_the_coordinator_read(self):
        self.report()
        for name in ("report-claude.md", "metrics-claude.json", "compare-claude.html", "key.json"):
            self.assertTrue((self.dir / name).is_file(), name)
        text = (self.dir / "report-claude.md").read_text(encoding="utf-8")
        for heading in ("Условия допуска", "J0", "J1", "J2", "J3", "J4", "Рекомендация", "Расход по провайдерам", "SHA"):
            self.assertIn(heading, text, heading)

    def test_gates_judgements_and_the_recommendation_follow_the_designed_rules(self):
        metrics = self.report()
        for config in ("G3", "H3"):
            self.assertTrue(metrics["gates"][config]["all"], metrics["gates"][config])
        self.assertEqual((metrics["j1"]["decision"], metrics["j1"]["deeper"], metrics["j1"]["resolved"]), ("H3", "H3", 24))
        self.assertFalse(metrics["j1"]["family_split"])
        self.assertTrue(metrics["j2"]["G"]["keep_v3"])
        self.assertTrue(metrics["j2"]["H"]["keep_v3"])
        self.assertEqual((metrics["j3"]["G"]["choice"], metrics["j3"]["H"]["choice"]), ("G3", "H3"))
        self.assertEqual(metrics["final"], {"g": "G3", "h": "H3"})
        self.assertEqual(metrics["recommendation"], {"choice": "H3", "basis": "j1", "owner_disagrees": False})
        self.assertEqual(metrics["j0"], {"openai": True, "claude": True})

    def test_attribution_is_reported_per_configuration_and_marked_as_reference_only(self):
        j4 = self.report()["j4"]
        self.assertEqual(j4["reference_only"], True)
        self.assertEqual(j4["by_config"]["G3"]["share"], 0.75)
        self.assertEqual(j4["by_config"]["H3"]["share"], 1.0)
        self.assertEqual(j4["by_config"]["C0"]["share"], 0.25)
        self.assertEqual(j4["by_config"]["G3"]["judge"], "claude")
        self.assertEqual(j4["by_config"]["H3"]["judge"], "openai")

    def test_cost_per_thousand_answers_time_to_first_word_and_the_two_template_measures(self):
        metrics = self.report()
        self.assertAlmostEqual(metrics["cost_per_1000_answers"]["G3"], 2.0, places=4)
        self.assertAlmostEqual(metrics["cost_per_1000_answers"]["H3"], 1.3, places=4)
        self.assertEqual(metrics["ttfb_ms"]["H3"], {"median": 1500, "p90": 1500})
        self.assertEqual(metrics["s1_word_overlap"]["G3"], 1.0)                 # four identical template answers
        self.assertEqual(metrics["s1_word_overlap"]["H3"], 0.0)                 # four different ones
        self.assertGreater(metrics["vam_stoit_share"]["H3"], 0.7)
        self.assertTrue(metrics["vam_stoit_new_template"]["H3"])
        self.assertEqual(metrics["vam_stoit_share"]["G3"], 0.0)
        self.assertFalse(metrics["vam_stoit_new_template"]["G3"])

    def test_the_spend_is_shown_per_provider_and_all_the_checkouts_by_sha(self):
        metrics = self.report()
        self.assertEqual(metrics["spend_by_provider"], {"openai": 1.2, "anthropic": 0.8})
        self.assertEqual(metrics["limits"], {"openai": 8.5, "anthropic": 9.5})
        self.assertEqual(sorted(metrics["shas"]), sorted(CONFIGS) + ["tool"])
        self.assertEqual(metrics["shas"]["G3"], "%040d" % 3)

    def test_the_owners_line_is_read_and_a_strong_disagreement_with_the_judges_is_reported(self):
        self.report()
        key = json.loads((self.dir / "key.json").read_text(encoding="utf-8"))
        sides = key["pages"]["compare-claude.html"]
        votes = []
        for q in Q.CLAUDE_PAGE_IDS[:9]:
            position = "1" if sides[q]["1"] == "G3" else "2"                  # the owner prefers G3 nine times
            votes.append("%s:%s" % (q, position))
        metrics = self.report("--owner-line", " ".join(votes))
        self.assertEqual(metrics["owner"]["tally"]["G3"], 9)
        self.assertEqual(metrics["recommendation"]["choice"], "H3")
        self.assertTrue(metrics["recommendation"]["owner_disagrees"])
        self.assertIn("судьи не отражают вкус владельца", (self.dir / "report-claude.md").read_text(encoding="utf-8"))

    def test_the_agreement_of_the_judges_with_the_owner_is_counted_on_the_pairs_both_have_decided(self):
        self.report()
        key = json.loads((self.dir / "key.json").read_text(encoding="utf-8"))
        sides = key["pages"]["compare-claude.html"]
        votes = []
        for q in Q.CLAUDE_PAGE_IDS:
            index = Q.EVALUATED_IDS.index(q)
            winner = "H3" if index < 20 else "G3" if index < 24 else None          # what the fabricated jury decided
            votes.append("%s:%s" % (q, "=" if winner is None else "1" if sides[q]["1"] == winner else "2"))
        agreement = self.report("--owner-line", " ".join(votes))["owner"]["agreement_with_jury"]
        self.assertEqual(set(agreement), {"agree", "compared"})
        self.assertEqual((agreement["agree"], agreement["compared"]), (11, 11))      # S1-B: the jury has no winner, the owner a tie

    def test_the_final_configurations_can_be_named_and_an_unknown_name_is_refused(self):
        metrics = self.report("--g-final", "G3x", "--h-final", "H3x")
        self.assertEqual(metrics["final"], {"g": "G3x", "h": "H3x"})
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()):
            code = None
            with self.assertRaises(SystemExit) as caught:
                cli.main(["report", "--claude", "--run-dir", str(self.dir), "--g-final", "G9"])
        self.assertEqual(caught.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
