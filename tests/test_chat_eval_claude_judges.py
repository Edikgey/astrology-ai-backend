"""CHAT-CLAUDE step 3, E-6..E-8: the rubric v2 and the two judges, the jury rules of DESIGN 6.4 (J0..J3, the family flag,
the recommendation) on synthetic tables of verdicts, and the masking and counting of the attribution check (J4).

Pure functions on tables, plus the Claude judge request against the real SDK and a loopback fake. No real API call.
"""
import json
import tempfile
import unittest

import chat_eval_secrets_guard  # noqa: F401  (no test of this run may open .secrets; tests/chat_eval_secrets_guard.py)
from pathlib import Path

import anthropic

from chat_eval_fake_server import FakeAnthropic, RUBRIC_V2, judgment
from tools.chat_eval import budget, facts as facts_mod, judge, metered
from tools.chat_eval import questions as Q


def lazy(name):
    """A module that does not exist yet fails inside each test that uses it, not at the import of this file."""
    import importlib

    class Lazy:
        def __getattr__(self, attr):
            return getattr(importlib.import_module("tools.chat_eval." + name), attr)
    return Lazy()


attribution, controls, jury = lazy("attribution"), lazy("controls"), lazy("jury")

HERE = Path(__file__).resolve().parent
CHARTS = json.loads((HERE.parent / "tools" / "chat_eval" / "charts.json").read_text(encoding="utf-8"))
OPUS = "claude-opus-5-5"


def natal_facts():
    return {"natal": {k: facts_mod.ChartFacts(v).for_judge() for k, v in CHARTS.items()}, "pairs": {}}


# ----------------------------------------------------------------------------------------------------- E-7: the rubric
class RubricV2Tests(unittest.TestCase):
    def test_there_are_eight_criteria_with_depth_and_balance(self):
        self.assertEqual(list(judge.CRITERIA), list(RUBRIC_V2))
        self.assertEqual(len(judge.CRITERIA), 8)

    def test_the_judge_prompt_describes_both_new_criteria_with_anchors_and_the_four_cautions(self):
        text = judge.SYSTEM_PROMPT
        self.assertIn("eight criteria", text)
        self.assertNotIn("six criteria", text)
        for anchor in ("- depth:", "- balance:", "1 ", "3 ", "5 "):
            self.assertIn(anchor, text)
        lowered = text.lower()
        for caution in ("not reward a difficulty", "not lower", "вам стоит", "concrete"):
            self.assertIn(caution, lowered, caution)
        self.assertIn("another chart", lowered)                                  # depth 5: would be wrong for another chart
        self.assertIn("flattery", lowered)                                       # balance 1: flattery or scaring

    def test_the_schema_has_nothing_the_anthropic_structured_output_does_not_support(self):
        self.assertEqual(judge.schema_issues(judge.JUDGE_SCHEMA), [])
        self.assertEqual(judge.JUDGE_FORMAT["json_schema"]["schema"], judge.JUDGE_SCHEMA)
        scores = judge.JUDGE_SCHEMA["properties"]["answer_1"]["properties"]["scores"]
        self.assertEqual(sorted(scores["required"]), sorted(RUBRIC_V2))
        self.assertEqual(scores["properties"]["depth"], {"type": "integer", "enum": [1, 2, 3, 4, 5]})
        self.assertEqual(scores["properties"]["balance"], {"type": "integer", "enum": [1, 2, 3, 4, 5]})

    def test_the_schema_check_really_finds_unsupported_constructs(self):
        bad = {"type": "object", "additionalProperties": False, "required": ["x"], "properties": {
            "x": {"type": "array", "minItems": 2, "maxItems": 3, "items": {"type": "string", "minLength": 1, "title": "t"}},
            "y": {"type": "integer", "minimum": 1, "maximum": 5},
            "z": {"type": "object", "properties": {"q": {"type": "string"}}}}}
        issues = judge.schema_issues(bad)
        for keyword in ("minItems", "maxItems", "minLength", "title", "minimum", "maximum", "additionalProperties"):
            self.assertTrue(any(keyword in issue for issue in issues), (keyword, issues))
        self.assertEqual(judge.schema_issues({"type": "array", "minItems": 1, "items": {"type": "string"}}), [])   # minItems 0/1 are allowed

    def test_the_parser_rejects_an_answer_without_depth_or_balance(self):
        good = judgment("1")
        self.assertIsNotNone(judge.parse_judgment(json.dumps(good)))
        for missing in ("depth", "balance"):
            broken = json.loads(json.dumps(good))
            del broken["answer_1"]["scores"][missing]
            self.assertIsNone(judge.parse_judgment(json.dumps(broken)), missing)
        broken = json.loads(json.dumps(good))
        broken["answer_2"]["scores"]["balance"] = 0
        self.assertIsNone(judge.parse_judgment(json.dumps(broken)))

    def left_right(self):
        base = {"q": "N17", "run": 1, "question": Q.question_text("N17"), "subject": "natal", "chart": "A", "conv": "A3",
                "seeded_history": False, "history": []}
        return {**base, "config": "G3", "response": "Ответ джи"}, {**base, "config": "H3", "response": "Ответ аш"}

    def test_the_claude_request_has_the_designed_shape(self):
        left, right = self.left_right()
        request = judge.build_claude_request(left, right, natal_facts(), model=OPUS, effort="low")
        self.assertEqual(request["model"], OPUS)
        self.assertEqual(request["max_tokens"], 5000)                            # 3000 for the answer + 2000 for thinking
        self.assertEqual(request["thinking"], {"type": "adaptive"})
        self.assertEqual(request["output_config"], {"effort": "low", "format": {"type": "json_schema", "schema": judge.JUDGE_SCHEMA}})
        for forbidden in ("temperature", "top_p", "top_k", "fallbacks", "tools", "tool_choice", "betas", "metadata", "stop_sequences"):
            self.assertNotIn(forbidden, request, forbidden)
        self.assertEqual(request["system"][0]["text"], judge.SYSTEM_PROMPT)
        marks = json.dumps(request).count('"cache_control"')
        self.assertEqual(marks, 2)                                               # the rubric and the facts of the chart
        self.assertEqual(request["system"][0]["cache_control"], {"type": "ephemeral"})
        self.assertEqual(request["messages"][0]["role"], "user")
        blocks = request["messages"][0]["content"]
        self.assertEqual(blocks[0]["cache_control"], {"type": "ephemeral"})
        self.assertNotIn("cache_control", blocks[-1])
        self.assertNotIn("G3", json.dumps(request))                              # blind: no configuration label
        self.assertNotIn("H3", json.dumps(request))

    def test_the_facts_block_is_the_same_bytes_for_two_questions_of_one_chart_so_the_cache_can_read_it(self):
        left, right = self.left_right()
        other_left, other_right = dict(left, q="N01", question=Q.question_text("N01")), dict(right, q="N01", question=Q.question_text("N01"))
        first = judge.build_claude_request(left, right, natal_facts(), model=OPUS, effort="low")
        second = judge.build_claude_request(other_left, other_right, natal_facts(), model=OPUS, effort="low")
        self.assertEqual(first["messages"][0]["content"][0], second["messages"][0]["content"][0])
        self.assertNotEqual(first["messages"][0]["content"][-1], second["messages"][0]["content"][-1])
        self.assertEqual(first["system"], second["system"])

    def run_claude(self, server, order="XY"):
        left, right = self.left_right()
        with tempfile.TemporaryDirectory() as tmp:
            client = anthropic.Anthropic(api_key="sk-ant-test-" + "k" * 20, base_url=server.url, max_retries=0, timeout=20)
            counted = metered.MeteredAnthropic(client, budget.Budget(5.0, Path(tmp) / "s.jsonl", provider="anthropic"),
                                               metered.JsonlWriter(Path(tmp) / "c.jsonl"), config="judge")
            record = judge.judge_one(counted, left, right, order, natal_facts(), judge="claude", model=OPUS)
            spent = counted.budget.spent()
        return record, spent

    def test_a_valid_claude_reply_maps_the_verdict_back_in_both_orders_and_is_marked_with_its_judge(self):
        with FakeAnthropic(judge_valid=True, judge_winner="2") as server:
            forward, spent = self.run_claude(server, "XY")
            backward, _ = self.run_claude(server, "YX")
            self.assertEqual(server.count("judge"), 2)
            self.assertEqual(server.bodies[0]["model"], OPUS)
        self.assertEqual((forward["outcome"], forward["judge"], forward["winner"]), ("ok", "claude", "H3"))
        self.assertEqual((backward["outcome"], backward["winner"]), ("ok", "G3"))
        self.assertEqual(set(forward["scores"]["G3"]), set(RUBRIC_V2))
        self.assertGreater(spent, 0)

    def test_a_reply_that_breaks_the_schema_a_refusal_and_a_rate_limit_are_recorded_and_not_retried(self):
        for mode, judge_valid, expected in (("ok", False, "invalid_output"), ("refusal", True, "invalid_output"),
                                            ("rate_limit", True, "rejected"), ("overloaded", True, "server_error")):
            with self.subTest(mode=mode), FakeAnthropic(mode=mode, judge_valid=judge_valid) as server:
                record, _ = self.run_claude(server)
                self.assertEqual(server.count(), 1)
                self.assertEqual((record["outcome"], record["judge"]), (expected, "claude"))

    def test_the_openai_judge_records_say_openai(self):
        from test_chat_eval_budget import FakeInner, response
        from tools.chat_eval.metered import JsonlWriter, MeteredClient
        left, right = self.left_right()
        reply = response(1000, 200, model="gpt-6.1-sol")
        reply.choices[0].message.content = json.dumps(judgment("1"))
        with tempfile.TemporaryDirectory() as tmp:
            counted = MeteredClient(FakeInner(lambda: reply), budget.Budget(5.0, Path(tmp) / "s.jsonl"),
                                    JsonlWriter(Path(tmp) / "c.jsonl"), config="judge")
            record = judge.judge_one(counted, left, right, "XY", natal_facts())
        self.assertEqual((record["outcome"], record["judge"], record["winner"]), ("ok", "openai", "G3"))


# ----------------------------------------------------------------------------------------------------- E-6: the jury
def verdict(judge_name, q, order, winner, left, right, depth=(4, 4), balance=(4, 4), facts=(4, 4), boundary=("n/a", "n/a"), outcome="ok"):
    def scores(index):
        return {c: {"depth": depth[index], "balance": balance[index], "facts": facts[index]}.get(c, 4) for c in RUBRIC_V2}
    return {"judge": judge_name, "q": q, "run": 1, "left": left, "right": right, "order": order, "outcome": outcome, "winner": winner,
            "reason": "r", "scores": {left: scores(0), right: scores(1)}, "fact_errors": {left: [], right: []},
            "boundary_kept": {left: boundary[0], right: boundary[1]}}


def both_orders(judge_name, q, left, right, outcome, **kw):
    """outcome: left | right | tie | split (the two orders disagree) | invalid."""
    if outcome == "split":
        return [verdict(judge_name, q, "XY", left, left, right, **kw), verdict(judge_name, q, "YX", right, left, right, **kw)]
    if outcome == "invalid":
        return [verdict(judge_name, q, "XY", "tie", left, right, outcome="invalid_output", **kw),
                verdict(judge_name, q, "YX", "tie", left, right, **kw)]
    winner = {"left": left, "right": right, "tie": "tie"}[outcome]
    return [verdict(judge_name, q, o, winner, left, right, **kw) for o in ("XY", "YX")]


def table(left, right, plan, judges=("openai", "claude"), **kw):
    """plan: {q: outcome} or {q: (outcome for openai, outcome for claude)}."""
    rows = []
    for q, outcome in plan.items():
        for index, name in enumerate(judges):
            rows += both_orders(name, q, left, right, outcome[index] if isinstance(outcome, tuple) else outcome, **kw)
    return rows


QS = ["Q%02d" % i for i in range(28)]


def plan(**counts):
    """{'left': n, 'right': m, 'tie': k, 'split': s} -> {q: outcome}, in order."""
    result, index = {}, 0
    for outcome, number in counts.items():
        for _ in range(number):
            result[QS[index]] = outcome
            index += 1
    return result


class JuryTests(unittest.TestCase):
    G, H = "G3", "H3"

    def choice(self, rows):
        return jury.model_choice(rows, self.G, self.H)

    def test_a_question_is_resolved_only_when_both_judges_name_the_same_winner_in_both_orders(self):
        rows = table(self.G, self.H, {"Q00": "right", "Q01": "left", "Q02": ("right", "left"), "Q03": ("right", "split"),
                                      "Q04": ("right", "tie"), "Q05": ("right", "invalid"), "Q06": ("left", "left")})
        resolved = jury.jury_pairs(rows, self.G, self.H)
        self.assertEqual({q: resolved[q] for q in sorted(resolved)},
                         {"Q00": self.H, "Q01": self.G, "Q02": "tie", "Q03": "tie", "Q04": "tie", "Q05": "invalid", "Q06": self.G})

    def test_old_rows_without_a_judge_field_are_openai(self):
        rows = both_orders("openai", "Q00", self.G, self.H, "right")
        for row in rows:
            del row["judge"]
        self.assertEqual(jury.pair_verdicts(rows, self.G, self.H, "openai"), {("Q00", 1): self.H})
        self.assertEqual(jury.pair_verdicts(rows, self.G, self.H, "claude"), {})

    def test_a_clear_winner_needs_12_resolved_two_thirds_and_a_margin_of_6(self):
        rows = table(self.G, self.H, plan(right=20, left=4, tie=4))
        result = self.choice(rows)
        self.assertEqual((result["resolved"], result["wins"], result["decision"]), (24, {self.G: 4, self.H: 20}, self.H))
        rows = table(self.G, self.H, plan(right=10, left=3, tie=15))                  # 13 resolved, 10 vs 3: margin 7
        self.assertEqual(self.choice(rows)["decision"], self.H)
        rows = table(self.G, self.H, plan(right=9, left=4, tie=15))                   # 13 resolved, 9 vs 4: margin 5
        self.assertEqual(self.choice(rows)["decision"], "unclear")
        rows = table(self.G, self.H, plan(right=8, left=4, tie=16))                   # 12 resolved, exactly 2/3, margin 4
        self.assertEqual(self.choice(rows)["decision"], "unclear")
        rows = table(self.G, self.H, plan(right=11, left=0, tie=17))                  # 11 resolved: not enough
        self.assertEqual(self.choice(rows)["decision"], "unclear")
        rows = table(self.G, self.H, plan(left=19, right=3, tie=6))                   # the other way round
        self.assertEqual(self.choice(rows)["decision"], self.G)
        rows = table(self.G, self.H, plan(right=13, left=7, tie=8))                   # 13/20 = 65% < 2/3
        self.assertEqual(self.choice(rows)["decision"], "unclear")

    def test_the_family_flag_fires_when_each_judge_prefers_its_own_family_and_the_verdict_becomes_unclear(self):
        rows = []
        for q, outcome in plan(a=20, tie=8).items():
            rows += both_orders("openai", q, self.G, self.H, "left" if outcome == "a" else "tie")
            rows += both_orders("claude", q, self.G, self.H, "right" if outcome == "a" else "tie")
        result = self.choice(rows)
        self.assertTrue(result["family_split"])
        self.assertEqual(result["decision"], "unclear")
        self.assertEqual(result["judge_shares"]["openai"], {"resolved": 20, "left_share": 1.0})
        self.assertEqual(result["judge_shares"]["claude"], {"resolved": 20, "left_share": 0.0})
        agreeing = self.choice(table(self.G, self.H, plan(right=20, tie=8)))
        self.assertFalse(agreeing["family_split"])
        mild = []
        for q, outcome in plan(a=10, b=10, tie=8).items():                           # openai: 50% its own: under 60%
            mild += both_orders("openai", q, self.G, self.H, "left" if outcome == "a" else "right" if outcome == "b" else "tie")
            mild += both_orders("claude", q, self.G, self.H, "right" if outcome != "tie" else "tie")
        self.assertFalse(self.choice(mild)["family_split"])

    def test_deeper_and_more_honest_need_a_gap_of_0_4_at_both_judges(self):
        rows = table(self.G, self.H, plan(right=14, tie=14), depth=(4.0, 4.5), balance=(3.0, 3.4))
        result = self.choice(rows)
        self.assertEqual((result["deeper"], result["more_honest"]), (self.H, self.H))
        one_judge = table(self.G, self.H, plan(right=14, tie=14), judges=("openai",), depth=(4.0, 4.5)) + \
            table(self.G, self.H, plan(right=14, tie=14), judges=("claude",), depth=(4.0, 4.2))
        self.assertIsNone(self.choice(one_judge)["deeper"])
        swapped = table(self.G, self.H, plan(left=14, tie=14), depth=(4.6, 4.0), balance=(4.4, 4.0))
        self.assertEqual((self.choice(swapped)["deeper"], self.choice(swapped)["more_honest"]), (self.G, self.G))
        self.assertIsNone(self.choice(table(self.G, self.H, plan(right=14, tie=14)))["deeper"])


class PromptEffectTests(unittest.TestCase):
    V2, V3 = "G2", "G3"

    def rows(self, v3_wins=18, v2_wins=4, tie=6, depth=(3.5, 4.0), balance=(3.5, 4.0), facts=(4.0, 4.0), bad_boundary=False):
        rows = []
        for q, outcome in plan(right=v3_wins, left=v2_wins, tie=tie).items():
            rows += both_orders("openai", q, self.V2, self.V3, outcome, depth=depth, balance=balance, facts=facts)
        for q in ("N13", "N14", "R05"):
            rows += both_orders("openai", q, self.V2, self.V3, "tie", depth=depth, balance=balance, facts=facts,
                                boundary=("yes", "no" if bad_boundary else "yes"))
        return rows

    def test_v3_stays_when_every_condition_holds(self):
        result = jury.prompt_effect(self.rows(), self.V2, self.V3, gates_ok=True)
        self.assertTrue(result["keep_v3"], result)
        self.assertEqual(result["reasons"], [])
        self.assertEqual(result["resolved"], 22)
        self.assertAlmostEqual(result["v3_share"], 18 / 22, places=4)

    def test_each_failed_condition_is_named(self):
        cases = {
            "resolved": self.rows(v3_wins=8, v2_wins=3, tie=17),
            "share": self.rows(v3_wins=10, v2_wins=8, tie=10),
            "depth": self.rows(depth=(3.7, 4.1)),
            "balance": self.rows(balance=(3.7, 4.1)),
            "facts": self.rows(facts=(4.0, 3.6)),
            "boundary": self.rows(bad_boundary=True),
        }
        for reason, rows in cases.items():
            with self.subTest(reason=reason):
                result = jury.prompt_effect(rows, self.V2, self.V3, gates_ok=True)
                self.assertFalse(result["keep_v3"])
                self.assertIn(reason, result["reasons"])
        self.assertIn("gates", jury.prompt_effect(self.rows(), self.V2, self.V3, gates_ok=False)["reasons"])
        self.assertTrue(jury.prompt_effect(self.rows(facts=(4.0, 3.75)), self.V2, self.V3, gates_ok=True)["keep_v3"])     # -0.25 is allowed

    def test_only_the_openai_judge_counts_for_j2(self):
        rows = self.rows()
        claude = [dict(r, judge="claude", winner=self.V2) for r in rows]
        self.assertTrue(jury.prompt_effect(rows + claude, self.V2, self.V3, gates_ok=True)["keep_v3"])

    def test_the_robustness_check_drops_conversation_a_and_the_s1_a_answer(self):
        self.assertEqual(set(jury.J2_ROBUST_EXCLUDE), {"N01", "N02", "N03", "S1-A"})
        rows = []
        wins_only_there = {q: "right" for q in jury.J2_ROBUST_EXCLUDE}
        rest = {"Q%02d" % i: ("tie") for i in range(20)}
        for q, outcome in {**wins_only_there, **rest}.items():
            rows += both_orders("openai", q, self.V2, self.V3, outcome, depth=(3.5, 4.5), balance=(3.5, 4.5))
        wide = jury.prompt_effect_robust(self.rows(), self.V2, self.V3, gates_ok=True)
        self.assertTrue(wide["keep_v3"] and wide["stable"], wide)
        fragile = jury.prompt_effect_robust(rows, self.V2, self.V3, gates_ok=True)
        self.assertFalse(fragile["keep_v3_without_a"])
        self.assertFalse(fragile["stable"] and fragile["keep_v3"] and fragile["keep_v3_without_a"])
        self.assertIn("stable", fragile)


class EffortChoiceTests(unittest.TestCase):
    CHEAP, COSTLY = "H3", "H3x"

    def rows(self, costly_wins, cheap_wins, tie=0, extra_outside=0):
        s10 = list(Q.S10_IDS)
        plan_ = {}
        for q in s10:
            plan_[q] = "right" if costly_wins > 0 else "left" if cheap_wins > 0 else "tie"
            costly_wins, cheap_wins = (costly_wins - 1, cheap_wins) if plan_[q] == "right" else (costly_wins, cheap_wins - 1 if plan_[q] == "left" else cheap_wins)
        for q in s10[len(s10) - tie:]:
            plan_[q] = "tie"
        rows = []
        for q, outcome in plan_.items():
            rows += both_orders("claude", q, self.CHEAP, self.COSTLY, outcome)
        for q in QS[:extra_outside]:
            rows += both_orders("claude", q, self.CHEAP, self.COSTLY, "right")             # outside S10: must not count
        return rows

    def test_s10_is_the_ten_designed_questions(self):
        self.assertEqual(list(Q.S10_IDS), ["N01", "N03", "N05", "N08", "N11", "N13", "N17", "N18", "R02", "R06"])

    def test_the_costly_effort_must_win_70_percent_of_at_least_6_resolved_and_pass_its_own_gates(self):
        won = jury.effort_choice(self.rows(7, 3), self.CHEAP, self.COSTLY, costly_gates_ok=True, judge="claude")
        self.assertEqual((won["choice"], won["resolved"], won["share"]), (self.COSTLY, 10, 0.7))
        lost = jury.effort_choice(self.rows(6, 3), self.CHEAP, self.COSTLY, costly_gates_ok=True, judge="claude")    # 6/9 = 67%
        self.assertEqual(lost["choice"], self.CHEAP)
        few = jury.effort_choice(self.rows(5, 0, tie=5), self.CHEAP, self.COSTLY, costly_gates_ok=True, judge="claude")  # 5 resolved
        self.assertEqual((few["choice"], few["resolved"]), (self.CHEAP, 5))
        six = jury.effort_choice(self.rows(6, 0, tie=4), self.CHEAP, self.COSTLY, costly_gates_ok=True, judge="claude")
        self.assertEqual(six["choice"], self.COSTLY)
        gates = jury.effort_choice(self.rows(10, 0), self.CHEAP, self.COSTLY, costly_gates_ok=False, judge="claude")
        self.assertEqual((gates["choice"], gates["reason"]), (self.CHEAP, "gates"))

    def test_only_the_s10_questions_and_only_the_named_judge_count(self):
        rows = self.rows(3, 7, extra_outside=12)
        self.assertEqual(jury.effort_choice(rows, self.CHEAP, self.COSTLY, costly_gates_ok=True, judge="claude")["choice"], self.CHEAP)
        self.assertEqual(jury.effort_choice(rows, self.CHEAP, self.COSTLY, costly_gates_ok=True, judge="openai")["resolved"], 0)


def answers_for(config, n=28, **fields):
    rows = []
    for i in range(n):
        rows.append({"config": config, "q": QS[i], "run": 1, "outcome": "ok", "response": "Короткий ответ.", "cost_usd": 0.002,
                     "ttfb_ms": 1500, "follow_up_count": 3, "subject": "natal", "chart": "A", **fields})
    return rows


class AdmissionTests(unittest.TestCase):
    BASE = answers_for("C0", cost_usd=0.001, ttfb_ms=1000)
    GOOD = dict(contradiction_rate=0.04, baseline_rate=0.03, boundary={"openai": {"yes": 3, "no": 0}, "claude": {"yes": 3, "no": 0}})

    def check(self, rows, **override):
        return jury.admission(rows, self.BASE, **{**self.GOOD, **override})

    def test_a_good_candidate_passes_all_nine(self):
        result = self.check(answers_for("G3"))
        self.assertEqual(sorted(k for k in result if k.startswith("G")), ["G%d" % i for i in range(1, 10)])
        self.assertTrue(result["all"], result)

    def test_each_gate_has_its_own_failure(self):
        rows = answers_for("G3")

        def with_(index, **fields):
            changed = [dict(r) for r in rows]
            changed[index].update(fields)
            return changed
        self.assertFalse(self.check(with_(0, outcome="truncated"))["G1"])
        self.assertFalse(self.check(with_(0, outcome="filtered"))["G2"])
        self.assertTrue(self.check(with_(0, outcome="server_error"))["G3"])                     # one failure is allowed
        two = with_(0, outcome="server_error")
        two[1]["outcome"] = "interrupted"
        self.assertFalse(self.check(two)["G3"])
        self.assertTrue(self.check(with_(0, outcome="truncated"))["G3"])                       # truncation has its own gate
        self.assertFalse(self.check(rows, contradiction_rate=0.06)["G4"])
        self.assertFalse(self.check(rows, contradiction_rate=0.045, baseline_rate=0.03)["G4"])  # 1.5 points above the baseline
        self.assertTrue(self.check(rows, contradiction_rate=0.04, baseline_rate=0.03)["G4"])
        self.assertFalse(self.check(rows, boundary={"openai": {"yes": 2, "no": 1}, "claude": {"yes": 3, "no": 0}})["G5"])
        self.assertFalse(self.check(rows, boundary={"openai": {"yes": 0, "no": 0}, "claude": {"yes": 3, "no": 0}})["G5"])
        self.assertFalse(self.check(with_(0, response="| a | b |\n|---|---|\n| 1 | 2 |"))["G6"])
        three = [dict(r, follow_up_count=3) for r in rows]
        for i in range(2):
            three[i]["follow_up_count"] = 0
        self.assertTrue(self.check(three)["G7"])                                                # 26 of 28 = 92.9 percent
        three[2]["follow_up_count"] = 0
        self.assertFalse(self.check(three)["G7"])                                               # 25 of 28 = 89.3 percent
        self.assertTrue(self.check([dict(r, cost_usd=0.002) for r in rows])["G8"])              # exactly twice the baseline median
        self.assertFalse(self.check([dict(r, cost_usd=0.0021) for r in rows])["G8"])
        self.assertTrue(self.check([dict(r, ttfb_ms=3000) for r in rows])["G9"])                # baseline median 1000 + 2000
        self.assertFalse(self.check([dict(r, ttfb_ms=3001) for r in rows])["G9"])
        slow_tail = [dict(r) for r in rows]
        for i in range(4):
            slow_tail[i]["ttfb_ms"] = 12000
        self.assertFalse(self.check(slow_tail)["G9"])                                           # median fine, p90 over 10 s

    def test_the_blocked_x01_question_is_not_counted(self):
        rows = answers_for("G3") + [{"config": "G3", "q": "X01", "run": 1, "outcome": "blocked", "response": None}]
        self.assertTrue(self.check(rows)["all"])


class RecommendationTests(unittest.TestCase):
    def pick(self, **kw):
        args = dict(admitted={"G3": True, "H3": True}, j1_decision="unclear", owner=None, cost={"G3": 0.001, "H3": 0.001},
                    ttfb={"G3": 1000, "H3": 1000})
        args.update(kw)
        return jury.recommend("G3", "H3", **args)

    def test_nothing_admitted_and_only_one_admitted(self):
        self.assertEqual(self.pick(admitted={"G3": False, "H3": False})["choice"], None)
        self.assertEqual(self.pick(admitted={"G3": False, "H3": False})["basis"], "none_admitted")
        only = self.pick(admitted={"G3": False, "H3": True}, j1_decision="G3")
        self.assertEqual((only["choice"], only["basis"]), ("H3", "only_admitted"))

    def test_a_clear_j1_winner_wins_and_a_strong_disagreement_of_the_owner_is_shown(self):
        agreed = self.pick(j1_decision="H3", owner={"G3": 3, "H3": 7, "tie": 2})
        self.assertEqual((agreed["choice"], agreed["basis"], agreed["owner_disagrees"]), ("H3", "j1", False))
        against = self.pick(j1_decision="H3", owner={"G3": 8, "H3": 2, "tie": 2})
        self.assertEqual((against["choice"], against["owner_disagrees"]), ("H3", True))
        edge = self.pick(j1_decision="H3", owner={"G3": 7, "H3": 3, "tie": 2})
        self.assertFalse(edge["owner_disagrees"])

    def test_without_a_clear_winner_the_owner_decides_by_a_margin_of_3_ties_not_counted(self):
        by_owner = self.pick(owner={"G3": 7, "H3": 4, "tie": 1})
        self.assertEqual((by_owner["choice"], by_owner["basis"]), ("G3", "owner"))
        thin = self.pick(owner={"G3": 6, "H3": 4, "tie": 2}, cost={"G3": 0.0015, "H3": 0.001})
        self.assertEqual((thin["choice"], thin["basis"]), ("H3", "cost"))

    def test_then_the_cheaper_by_median_cost_and_within_20_percent_the_faster(self):
        cheaper = self.pick(cost={"G3": 0.0010, "H3": 0.0013})
        self.assertEqual((cheaper["choice"], cheaper["basis"]), ("G3", "cost"))
        close = self.pick(cost={"G3": 0.0010, "H3": 0.0011}, ttfb={"G3": 2500, "H3": 1800})
        self.assertEqual((close["choice"], close["basis"]), ("H3", "speed"))
        exactly = self.pick(cost={"G3": 0.0010, "H3": 0.0012}, ttfb={"G3": 2500, "H3": 1800})            # 20 percent: not under 20
        self.assertEqual(exactly["basis"], "cost")


# ----------------------------------------------------------------------------------------------------- J0: the controls
class ControlsTests(unittest.TestCase):
    def test_six_pairs_in_the_designed_places(self):
        kinds = {c["kind"]: c for c in controls.CONTROL_PAIRS}
        self.assertEqual(sorted(kinds), ["boundary", "depth", "doom", "facts", "filler", "flattery"])
        self.assertEqual({k: (c["q"], c["chart"]) for k, c in kinds.items()},
                         {"depth": ("S1-A", "A"), "flattery": ("N17", "A"), "doom": ("N11", "D"), "facts": ("N07", "B"),
                          "boundary": ("N13", "D"), "filler": ("N10", "C")})
        for c in controls.CONTROL_PAIRS:
            self.assertIn(c["q"], Q.QUESTIONS)
            self.assertNotEqual(c["bad"], c["good"])
            self.assertTrue(c["bad"].strip() and c["good"].strip())
        self.assertEqual((controls.BAD, controls.GOOD), ("bad", "good"))

    def test_the_wrong_facts_control_contradicts_the_chart_and_the_right_one_does_not(self):
        control = next(c for c in controls.CONTROL_PAIRS if c["kind"] == "facts")
        chart = facts_mod.ChartFacts(CHARTS[control["chart"]])
        bad, good = facts_mod.check_answer(control["bad"], chart), facts_mod.check_answer(control["good"], chart)
        self.assertGreaterEqual(bad["contradicted"], 1, bad)
        self.assertEqual(good["contradicted"], 0, good)
        self.assertGreaterEqual(good["confirmed"], 1, good)

    def test_the_long_empty_answer_is_much_longer_than_the_short_precise_one(self):
        control = next(c for c in controls.CONTROL_PAIRS if c["kind"] == "filler")
        self.assertGreaterEqual(len(control["bad"]), 2.5 * len(control["good"]))

    def test_the_flattery_and_doom_answers_have_no_difficulty_and_a_verdict_the_good_ones_avoid(self):
        by = {c["kind"]: c for c in controls.CONTROL_PAIRS}
        self.assertIn("вам стоит", by["flattery"]["good"].lower())
        self.assertNotIn("вам стоит", by["flattery"]["bad"].lower())
        self.assertRegex(by["doom"]["bad"].lower(), r"обречен|обреч|никогда|всегда будет")
        self.assertNotRegex(by["doom"]["good"].lower(), r"обречен|обреч")
        self.assertRegex(by["boundary"]["bad"].lower(), r"вложи|вкладывай|стоит вложить|вкладывать")

    def test_the_two_attribution_controls(self):
        by = {c["id"]: c for c in controls.ATTRIBUTION_CONTROLS}
        self.assertEqual(sorted(by), ["AC1", "AC2"])
        self.assertEqual((by["AC1"]["chart_x"], by["AC1"]["chart_y"]), ("A", "D"))
        self.assertIn("Рыб", by["AC1"]["answer_x"])
        self.assertIn("Близнец", by["AC1"]["answer_y"])
        self.assertEqual(by["AC2"]["answer_x"], by["AC2"]["answer_y"])                    # the same template for both charts

    def test_the_control_records_are_ready_for_the_judge_and_blind(self):
        records = controls.control_records()
        self.assertEqual(len(records), 12)
        self.assertEqual({r["config"] for r in records}, {"bad", "good"})
        for r in records:
            self.assertEqual({"q", "run", "config", "response", "question", "subject", "chart", "conv", "history", "seeded_history"} - set(r), set())
            self.assertEqual(r["question"], Q.question_text(r["q"]))

    def j0_rows(self, judge_name="openai", wrong=(), depth_of_bad=1, balance_of_bad=1, facts_of_bad=1, boundary_of_bad="no", errors=True):
        rows = []
        for c in controls.CONTROL_PAIRS:
            for order in ("XY", "YX"):
                winner = controls.BAD if c["q"] in wrong else controls.GOOD
                scores = {controls.BAD: {k: 4 for k in RUBRIC_V2}, controls.GOOD: {k: 5 for k in RUBRIC_V2}}
                scores[controls.BAD]["depth"], scores[controls.BAD]["balance"], scores[controls.BAD]["facts"] = depth_of_bad, balance_of_bad, facts_of_bad
                rows.append({"judge": judge_name, "q": c["q"], "run": 1, "left": controls.BAD, "right": controls.GOOD, "order": order,
                             "outcome": "ok", "winner": winner, "scores": scores,
                             "fact_errors": {controls.BAD: [{"claim": "x", "why": "y"}] if errors and c["kind"] == "facts" else [], controls.GOOD: []},
                             "boundary_kept": {controls.BAD: boundary_of_bad if c["kind"] == "boundary" else "n/a",
                                               controls.GOOD: "yes" if c["kind"] == "boundary" else "n/a"}})
        return rows

    def attribution_rows(self, ac1="right", ac2="cannot_tell"):
        rows = []
        for order, truth in (("XY", "1"), ("YX", "2")):
            rows.append({"control": "AC1", "order": order, "truth": truth, "verdict": truth if ac1 == "right" else "cannot_tell"})
            rows.append({"control": "AC2", "order": order, "truth": "cannot_tell", "verdict": ac2})
        return rows

    def test_the_calibration_passes_when_the_judge_picks_5_of_6_and_all_the_criteria_checks_hold(self):
        ok = controls.evaluate_j0(self.j0_rows(), self.attribution_rows())
        self.assertTrue(ok["passed"], ok)
        self.assertEqual(ok["pairs_ok"], 6)
        one_wrong = controls.evaluate_j0(self.j0_rows(wrong=("N10",)), self.attribution_rows())
        self.assertTrue(one_wrong["passed"])
        self.assertEqual(one_wrong["pairs_ok"], 5)
        two_wrong = controls.evaluate_j0(self.j0_rows(wrong=("N10", "N07")), self.attribution_rows())
        self.assertFalse(two_wrong["passed"])

    def test_a_pair_counts_only_when_both_orders_pick_the_good_answer(self):
        rows = self.j0_rows()
        for row in rows:
            if row["q"] == "N10" and row["order"] == "YX":
                row["winner"] = controls.BAD
        self.assertEqual(controls.evaluate_j0(rows, self.attribution_rows())["pairs_ok"], 5)
        rows[0]["outcome"] = "invalid_output"
        self.assertLessEqual(controls.evaluate_j0(rows, self.attribution_rows())["pairs_ok"], 5)

    def test_the_criteria_checks(self):
        cases = {"depth": dict(depth_of_bad=3), "balance": dict(balance_of_bad=3), "facts": dict(facts_of_bad=3),
                 "fact_errors": dict(errors=False), "boundary": dict(boundary_of_bad="yes")}
        for name, kw in cases.items():
            with self.subTest(name=name):
                result = controls.evaluate_j0(self.j0_rows(**kw), self.attribution_rows())
                self.assertFalse(result["passed"], name)
                self.assertTrue(any(name.split("_")[0] in failed for failed in result["failed"]), result["failed"])

    def test_both_attribution_controls_must_be_right(self):
        self.assertFalse(controls.evaluate_j0(self.j0_rows(), self.attribution_rows(ac1="blind"))["passed"])
        self.assertFalse(controls.evaluate_j0(self.j0_rows(), self.attribution_rows(ac2="1"))["passed"])
        self.assertFalse(controls.evaluate_j0(self.j0_rows(), [])["passed"])


# ----------------------------------------------------------------------------------------------------- E-8: attribution
MASK_CASES = [
    ("Ваше Солнце в Рыбах даёт мягкость.", ["Солнце", "Рыбах"], ["Ваше", "даёт", "мягкость"]),
    ("Луна в Водолее стоит в 12 доме.", ["Луна", "Водолее", "12"], ["стоит"]),
    ("Асцендент в Овне делает вас прямым.", ["Асцендент", "Овне"], ["делает", "прямым"]),
    ("Юпитер в оппозиции к Сатурну с орбисом 0.3° задаёт напряжение.", ["Юпитер", "оппозиции", "Сатурну", "0.3", "орбис"], ["задаёт", "напряжение"]),
    ("Меркурий в 4°18′ Овна говорит быстро.", ["Меркурий", "4°18", "Овна"], ["говорит", "быстро"]),
    ("Венера в Деве любит порядок.", ["Венера", "Деве"], ["любит", "порядок"]),
    ("Марс квадрат Солнце с орбисом 3 градуса создаёт энергию.", ["Марс", "квадрат", "Солнце", "3 градус", "орбис"], ["создаёт", "энергию"]),
    ("Средина Неба в Козероге указывает на карьеру.", ["Средина Неба", "Козероге"], ["указывает", "карьеру"]),
    ("MC в Козероге про цели.", ["MC", "Козероге"], ["цели"]),
    ("Северный узел в Близнецах зовёт к общению.", ["Северный узел", "Близнецах"], ["зовёт", "общению"]),
    ("Чёрная Луна Лилит в Скорпионе пугает людей.", ["Луна", "Лилит", "Скорпионе"], ["пугает", "людей"]),
    ("Стеллиум в Деве в 5 доме собирает фокус.", ["Стеллиум", "Деве", "5 доме"], ["собирает", "фокус"]),
    ("Сатурн в седьмом доме учит ответственности.", ["Сатурн", "седьмом"], ["учит", "ответственности"]),
    ("Плутон трин Венера (орбис 1.4°) даёт глубину.", ["Плутон", "трин", "Венера", "1.4", "орбис"], ["даёт", "глубину"]),
    ("Нептун в Козероге и Уран в Водолее.", ["Нептун", "Козероге", "Уран", "Водолее"], []),
    ("Ретроградный Юпитер ведёт внутрь.", ["Ретроградный", "Юпитер"], ["ведёт", "внутрь"]),
    ("Секстиль Луны и Венеры смягчает характер.", ["Секстиль", "Луны", "Венеры"], ["смягчает", "характер"]),
    ("Соединение Солнца и Меркурия усиливает речь.", ["Соединение", "Солнца", "Меркурия"], ["усиливает", "речь"]),
    ("Рак с Весами дружит, Лев не против.", ["Рак", "Весами", "Лев"], ["дружит", "против"]),
    ("Знаки Телец и Стрелец упрямы.", ["Телец", "Стрелец"], ["Знаки", "упрямы"]),
    ("Луна находится на 17°47′ Тельца в 12-м доме.", ["Луна", "17°47", "Тельца", "12-м"], ["находится"]),
    ("Солнце в 29 градусах Близнецов — на пороге.", ["Солнце", "29 градус", "Близнецов"], ["пороге"]),
    ("Вертекс и Часть Фортуны добавляют красок.", ["Вертекс", "Фортуны"], ["добавляют", "красок"]),
    ("Южный узел в Рыбах напоминает о прошлом.", ["Южный узел", "Рыбах"], ["напоминает", "прошлом"]),
    ("Белая Луна Селена в Водолее светит.", ["Селена", "Водолее"], ["светит"]),
    ("Десцендент в Весах ищет баланс.", ["Десцендент", "Весах"], ["ищет", "баланс"]),
    ("Хирон в Деве — старая рана.", ["Хирон", "Деве"], ["старая", "рана"]),
    ("В первом доме Марс даёт напор.", ["Марс", "первом"], ["даёт", "напор"]),
    ("Ваш Меркурий в десятом доме болтлив.", ["Меркурий", "десятом"], ["болтлив"]),
    ("Полуквадрат Марса и Плутона нервирует.", ["Полуквадрат", "Марса", "Плутона"], ["нервирует"]),
    ("**Солнце в Рыбах** — это важно для вас.", ["Солнце", "Рыбах"], ["**", "важно", "вас"]),
    ("Your Sun in Pisces and Moon in Aquarius, 12th house, square Mars with orb 3.2°.",
     ["Sun", "Pisces", "Moon", "Aquarius", "12th", "square", "Mars", "3.2"], ["Your", "and", "with"]),
    ("Юпитер и Сатурн в 10 доме: Юпитер 3°58′ Льва.", ["Юпитер", "Сатурн", "10 доме", "3°58", "Льва"], []),
]
UNCHANGED = ["Сегодня хороший день, чтобы поговорить с близким человеком о важном.",
             "Вернитесь домой пораньше, дома вас ждут.",
             "Это даёт 3 шага: подумать, записать, сделать.",
             "Take a short walk and call a friend tomorrow.",
             "Спросите себя, что именно вас тревожит, и запишите ответ в три строки."]


class MaskTests(unittest.TestCase):
    def test_there_are_at_least_30_phrases(self):
        self.assertGreaterEqual(len(MASK_CASES) + len(UNCHANGED), 30)

    def test_names_degrees_and_orbs_are_hidden_and_ordinary_words_stay(self):
        for phrase, hidden, kept in MASK_CASES:
            masked = attribution.mask_text(phrase)
            lowered = masked.lower().replace("ё", "е")
            for word in hidden:
                self.assertNotIn(word.lower().replace("ё", "е"), lowered, (phrase, word, masked))
            for word in kept:
                self.assertIn(word, masked, (phrase, word, masked))
            self.assertIn(attribution.MASK_TOKEN, masked, phrase)

    def test_ordinary_text_is_not_touched(self):
        for phrase in UNCHANGED:
            self.assertEqual(attribution.mask_text(phrase), phrase)

    def test_masking_twice_changes_nothing_and_empty_input_is_safe(self):
        for phrase, _, _ in MASK_CASES:
            once = attribution.mask_text(phrase)
            self.assertEqual(attribution.mask_text(once), once, phrase)
        self.assertEqual(attribution.mask_text(""), "")
        self.assertEqual(attribution.mask_text(None), "")


class AttributionJobTests(unittest.TestCase):
    def s1_answers(self, config="G3"):
        texts = {"S1-A": "Ваше Солнце в Рыбах в 12 доме делает вас чувствительной, а Луна в Водолее ищет свободу.",
                 "S1-B": "Стеллиум в Деве в 5 доме, Солнце и Меркурий, даёт вам внимание к деталям.",
                 "S1-C": "Солнце в Стрельце в 9 доме тянет вас к смыслам; Луна во Льве греет окружающих.",
                 "S1-D": "Солнце в Близнецах на 29°05′ и Луна в Тельце дают любопытство и упрямство."}
        return [{"config": config, "q": q, "run": 1, "outcome": "ok", "response": text, "conv": "S" + q[-1],
                 "subject": "natal", "chart": q[-1], "history": [], "question": Q.question_text(q)} for q, text in texts.items()]

    def test_four_jobs_per_configuration_two_pairs_in_two_orders_and_the_answers_are_masked(self):
        jobs = attribution.attribution_jobs(self.s1_answers(), natal_facts(), "G3")
        self.assertEqual(len(jobs), 4)
        self.assertEqual(sorted({(j["chart_x"], j["chart_y"]) for j in jobs}), [("A", "D"), ("B", "C")])
        self.assertEqual(sorted({j["order"] for j in jobs}), ["XY", "YX"])
        for job in jobs:
            payload = json.loads(job["messages"][1]["content"])
            for position in ("answer_1", "answer_2"):
                text = payload[position].lower()
                for word in ("солнц", "лун", "рыб", "близнец", "стрельц", "стеллиум", "дев", "29°"):
                    self.assertNotIn(word, text, (position, word))
            self.assertIn("ATTRIBUTION TASK", job["messages"][0]["content"])
            self.assertEqual(sorted(payload["chart_x"]), sorted(payload["chart_y"]))     # the facts of both charts in the same layout
            self.assertNotIn("G3", job["messages"][1]["content"])
            self.assertIn(job["truth"], ("1", "2"))

    def test_the_truth_follows_the_order(self):
        jobs = attribution.attribution_jobs(self.s1_answers(), natal_facts(), "G3")
        by = {(j["chart_x"], j["order"]): j for j in jobs}
        xy, yx = by[("A", "XY")], by[("A", "YX")]
        self.assertEqual((xy["truth"], yx["truth"]), ("1", "2"))
        payload = json.loads(xy["messages"][1]["content"])
        self.assertEqual(payload["answer_1"], attribution.mask_text(self.s1_answers()[0]["response"]))

    def test_a_missing_answer_drops_only_its_pair(self):
        answers = [a for a in self.s1_answers() if a["q"] != "S1-C"]
        jobs = attribution.attribution_jobs(answers, natal_facts(), "G3")
        self.assertEqual(sorted({(j["chart_x"], j["chart_y"]) for j in jobs}), [("A", "D")])
        broken = self.s1_answers()
        broken[1]["outcome"] = "truncated"
        self.assertEqual(sorted({(j["chart_x"], j["chart_y"]) for j in attribution.attribution_jobs(broken, natal_facts(), "G3")}), [("A", "D")])

    def test_the_judge_of_each_family_is_the_other_one(self):
        self.assertEqual([attribution.attribution_judge_for(c) for c in ("C0", "G2", "G3", "G3x")], ["claude"] * 4)
        self.assertEqual([attribution.attribution_judge_for(c) for c in ("H2", "H3", "H3x")], ["openai"] * 3)

    def test_the_reply_is_parsed_strictly(self):
        for good in ("1", "2", "cannot_tell"):
            self.assertEqual(attribution.parse_attribution(json.dumps({"answer_for_chart_x": good})), good)
        for bad in ('{"answer_for_chart_x": "3"}', '{"answer_for_chart_x": "1", "x": 1}', "not json", '{"answer": "1"}', None, '[]'):
            self.assertIsNone(attribution.parse_attribution(bad), bad)
        self.assertEqual(judge.schema_issues(attribution.ATTRIBUTION_SCHEMA), [])

    def test_the_count_in_both_orders(self):
        records = [{"truth": "1", "verdict": "1"}, {"truth": "2", "verdict": "2"}, {"truth": "1", "verdict": "2"},
                   {"truth": "2", "verdict": "cannot_tell"}, {"truth": "1", "verdict": None}]
        result = attribution.attribution_summary(records)
        self.assertEqual({k: result[k] for k in ("total", "correct", "wrong", "cannot_tell", "invalid")},
                         {"total": 5, "correct": 2, "wrong": 1, "cannot_tell": 1, "invalid": 1})
        self.assertEqual(result["share"], 0.4)
        self.assertIsNone(attribution.attribution_summary([])["share"])

    def test_one_job_through_the_claude_counter_records_the_verdict_and_the_truth(self):
        job = attribution.attribution_jobs(self.s1_answers(), natal_facts(), "G3")[0]
        with tempfile.TemporaryDirectory() as tmp, FakeAnthropic(judge_valid=True, attribution="1") as server:
            client = anthropic.Anthropic(api_key="sk-ant-test-" + "k" * 20, base_url=server.url, max_retries=0, timeout=20)
            counted = metered.MeteredAnthropic(client, budget.Budget(5.0, Path(tmp) / "s.jsonl", provider="anthropic"),
                                               metered.JsonlWriter(Path(tmp) / "c.jsonl"), config="judge")
            record = attribution.run_job(counted, job, judge="claude", model=OPUS)
            self.assertEqual(server.count("attribution"), 1)
            self.assertEqual(server.bodies[0]["max_tokens"], 1500)
        self.assertEqual((record["verdict"], record["truth"], record["judge"], record["config"]), ("1", job["truth"], "claude", "G3"))
        self.assertEqual(record["outcome"], "ok")


if __name__ == "__main__":
    unittest.main()
