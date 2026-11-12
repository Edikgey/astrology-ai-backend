"""CHAT-QUALITY-eval: the full cycle, the key never leaves its place, blind pages, judge rules. Local fake provider only."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from chat_eval_fake_server import FakeOpenAI, judgment
from test_chat_eval_budget import FakeInner, REPO, response, run_cli
from tools.chat_eval import common, questions
from tools.chat_eval.budget import Budget
from tools.chat_eval.judge import build_messages, judge_one, pair_verdict, parse_judgment
from tools.chat_eval.metered import JsonlWriter, MeteredClient
from tools.chat_eval.page import assign_sides, build_page, render_markup
from tools.chat_eval.report import judge_metrics, thresholds


def read_all_text(root, skip=()):
    for path in Path(root).rglob("*"):
        if path.is_file() and path.name not in skip:
            yield path, path.read_text(encoding="utf-8", errors="replace")


class CycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.run_dir = self.root / "run"

    def cycle(self, *flags, only="N01,N03,N04"):
        return run_cli("cycle", "--configs", "C0,C1", "--runs", "1", "--only", only, "--base-repo", str(REPO),
                       "--candidate-repo", str(REPO), "--run-dir", str(self.run_dir), "--seed", "test", *flags)

    def test_dry_cycle_reaches_the_page_and_an_unusable_judge_reply_is_invalid_output(self):
        with FakeOpenAI() as server:                      # the judge request gets the wrong schema, as the stand does
            result = self.cycle("--base-url", server.url)
            self.assertEqual(server.count("judge"), 6)
        self.assertEqual(result.returncode, 0, result.stdout[-1200:])
        for line in result.stdout.splitlines():
            self.assertTrue(line.startswith("chat_eval."), line)
        for name in ("answers.jsonl", "calls.jsonl", "judgments.jsonl", "facts.json", "metrics.json", "report.md",
                     "compare.html", "key.json", "meta-C0.json", "meta-C1.json", "spend.jsonl"):
            self.assertTrue((self.run_dir / name).is_file(), name)
        judgments = [json.loads(l) for l in (self.run_dir / "judgments.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual({j["outcome"] for j in judgments}, {"invalid_output"})
        metrics = json.loads((self.run_dir / "metrics.json").read_text(encoding="utf-8"))
        self.assertEqual(metrics["judges"][0]["resolved"], 0)
        self.assertEqual({t["name"]: t["result"] for t in metrics["thresholds"]}[
            "Судья: C1 выигрывает >= 65% решённых пар при >= 25 решённых"], "no_data")
        self.assertGreater(metrics["projection"]["likely_usd"], 0)

    def test_the_paid_path_uses_the_key_from_the_owner_file_and_never_prints_or_writes_it(self):
        key = "sk-test-" + uuid4().hex
        key_file = self.root / "openai-eval.env"
        key_file.write_text("OPENAI_API_KEY=%s\n" % key, encoding="utf-8")
        with FakeOpenAI(expected_key=key, judge_valid=True) as server:
            result = self.cycle("--real", "--real-base-url", server.url, "--key-file", str(key_file))
            self.assertTrue(server.auth_seen and all(server.auth_seen), "the provider must receive the key from the file")
        self.assertEqual(result.returncode, 0, result.stdout[-1200:])
        self.assertNotIn(key, result.stdout)
        self.assertNotIn(key, result.stderr)
        for path, text in read_all_text(self.root, skip=("openai-eval.env",)):
            self.assertNotIn(key, text, path.name)
        self.assertTrue((self.root / "spend.jsonl").is_file())            # the paid ledger sits next to the run folder
        self.assertFalse((self.root / "run.lock").exists())
        judgments = [json.loads(l) for l in (self.run_dir / "judgments.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual({j["outcome"] for j in judgments}, {"ok"})
        self.assertEqual({j["winner"] for j in judgments}, {"C0", "C1"})   # judge always says "answer 2": order decides it

    def test_a_provider_that_echoes_the_key_in_its_error_does_not_leak_it(self):
        key = "sk-test-" + uuid4().hex
        key_file = self.root / "openai-eval.env"
        key_file.write_text("OPENAI_API_KEY=%s\n" % key, encoding="utf-8")
        with FakeOpenAI(expected_key=key, mode="unauthorized_echo") as server:
            result = self.cycle("--real", "--real-base-url", server.url, "--key-file", str(key_file))
            made = server.count()
        self.assertEqual(result.returncode, 4, result.stdout[-1200:])         # aborted: the provider refuses the key
        self.assertEqual(made, 2)                                              # after two refusals no more calls
        self.assertNotIn(key, result.stdout + result.stderr)
        for path, text in read_all_text(self.root, skip=("openai-eval.env",)):
            self.assertNotIn(key, text, path.name)
            self.assertNotIn("Incorrect API key", text, path.name)

    def test_blind_pages_show_no_configuration_and_the_key_file_holds_the_mapping(self):
        with FakeOpenAI() as server:
            self.assertEqual(self.cycle("--base-url", server.url).returncode, 0)
        page = (self.run_dir / "compare.html").read_text(encoding="utf-8")
        for secret in ("C0", "C1", "gpt-", "candidate", "base", "sha", "http://", "https://", "src="):
            self.assertNotIn(secret, page, secret)
        self.assertIn("Скопировать итог", page)
        self.assertEqual(page.count('class="answer"'), 6)                      # three cards, two answers each
        key = json.loads((self.run_dir / "key.json").read_text(encoding="utf-8"))
        self.assertEqual(sorted(key["pages"]["compare.html"]), ["N01", "N03", "N04"])
        for sides in key["pages"]["compare.html"].values():
            self.assertEqual(sorted(sides.values()), ["C0", "C1"])


class StartConditionTests(unittest.TestCase):
    def test_dry_mode_refuses_an_address_that_is_not_this_computer(self):
        result = run_cli("run", "--configs", "C0", "--base-repo", str(REPO), "--candidate-repo", str(REPO),
                         "--base-url", "https://api.openai.com/v1", "--run-dir", tempfile.gettempdir() + "/chat-eval-never")
        self.assertEqual(result.returncode, 2)
        self.assertIn("reason=dry_needs_loopback_url", result.stdout)

    def test_the_paid_run_without_the_owners_key_file_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = run_cli("run", "--real", "--configs", "C0", "--base-repo", str(REPO), "--candidate-repo", str(REPO),
                             "--key-file", str(Path(tmp) / "openai-eval.env"), "--run-dir", str(Path(tmp) / "run"))
        self.assertEqual(result.returncode, 2)
        self.assertIn("reason=key_file_missing", result.stdout)

    def test_the_rehearsal_address_of_the_paid_path_must_be_loopback(self):
        with tempfile.TemporaryDirectory() as tmp:
            key_file = Path(tmp) / "openai-eval.env"
            key_file.write_text("OPENAI_API_KEY=sk-test-not-used-anywhere\n", encoding="utf-8")
            result = run_cli("run", "--real", "--real-base-url", "https://example.com/v1", "--configs", "C0",
                             "--base-repo", str(REPO), "--candidate-repo", str(REPO), "--key-file", str(key_file),
                             "--run-dir", str(Path(tmp) / "run"))
        self.assertEqual(result.returncode, 2)
        self.assertIn("reason=real_base_url_must_be_loopback", result.stdout)

    def test_the_dry_mode_socket_guard_refuses_outside_addresses(self):
        code = ("from tools.chat_eval.common import refuse_non_loopback\nimport socket\nrefuse_non_loopback()\n"
                "s = socket.socket()\ntry:\n    s.connect(('93.184.216.34', 80))\nexcept ConnectionRefusedError:\n    print('refused')\n")
        result = subprocess.run([sys.executable, "-c", code], cwd=str(REPO), capture_output=True, text=True, timeout=60)
        self.assertEqual(result.stdout.strip(), "refused")

    def test_a_paid_run_refuses_to_overlap_another(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "run.lock").write_text("", encoding="utf-8")
            key_file = Path(tmp) / "openai-eval.env"
            key_file.write_text("OPENAI_API_KEY=sk-test-not-used-anywhere\n", encoding="utf-8")
            result = run_cli("run", "--real", "--real-base-url", "http://127.0.0.1:9/v1", "--configs", "C0",
                             "--base-repo", str(REPO), "--candidate-repo", str(REPO), "--key-file", str(key_file),
                             "--run-dir", str(Path(tmp) / "run"))
        self.assertEqual(result.returncode, 2)
        self.assertIn("reason=another_run_in_progress", result.stdout)


class LogVocabularyTests(unittest.TestCase):
    def test_log_lines_carry_only_labels_and_numbers(self):
        import contextlib
        import io
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            common.log_event("chat_eval.answer", outcome="ok", q="N01", cost_usd=0.00123, input_tokens=100,
                             detail="Какой знак у моей Луны? секрет!", flag=True, none=None)
        line = buffer.getvalue().strip()
        self.assertEqual(line, "chat_eval.answer outcome=ok q=N01 cost_usd=0.00123 input_tokens=100 detail=invalid flag=true none=none")

    def test_a_registered_secret_is_masked_everywhere(self):
        secret = "sk-test-" + uuid4().hex
        common.register_secret(secret)
        self.addCleanup(common._SECRETS.discard, secret)
        self.assertEqual(common.redact("x %s y" % secret), "x [key] y")
        self.assertEqual(common.redact("Incorrect API key provided: sk-abcdefghijkl1234"), "Incorrect API key provided: [key]")

    def test_the_key_loader_reads_only_the_key_name_and_reports_closed_reasons(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "openai-eval.env"
            path.write_text("OTHER=1\nOPENAI_API_KEY=\n", encoding="utf-8")
            with self.assertRaises(common.KeyError_) as caught:
                common.load_key(path)
            self.assertEqual(str(caught.exception), "key_missing_in_file")
            with self.assertRaises(common.KeyError_) as caught:
                common.load_key(Path(tmp) / "nope.env")
            self.assertEqual(str(caught.exception), "key_file_missing")


class QuestionSetTests(unittest.TestCase):
    def test_the_set_matches_eval_set(self):
        self.assertEqual(len(questions.QUESTIONS), 21)
        self.assertEqual(sum(len(c[3]) for c in questions.CONVERSATIONS), 22)       # 21 evaluated + X01
        self.assertEqual(len(questions.OWNER_PAGE_IDS), 12)
        self.assertEqual(len(questions.SEED_MESSAGES), 24)
        self.assertEqual([m[0] for m in questions.SEED_MESSAGES[:4]], ["user", "gpt", "user", "gpt"])
        for _, answer in questions.SEED_PAIRS:
            self.assertTrue(500 <= len(answer) <= 900, len(answer))
        self.assertIn("работ", questions.SEED_PAIRS[0][0] + questions.SEED_PAIRS[0][1] + questions.SEED_PAIRS[1][0])
        self.assertEqual(questions.question_text("N07"), "Где у меня Юпитер и что он мне даёт?")

    def test_the_transit_guard_blocks_only_x01(self):
        os.environ.setdefault("DATABASE_URL", "sqlite://")
        from modules.ai_transit_guard import requires_transit_data
        self.assertTrue(requires_transit_data(questions.X01_QUESTION))
        for qid in questions.QUESTIONS:
            self.assertFalse(requires_transit_data(questions.question_text(qid)), qid)


class PageTests(unittest.TestCase):
    def test_the_markup_subset_is_drawn_and_nothing_else(self):
        self.assertEqual(render_markup("**Жирное** и *курсив*."), "<p><strong>Жирное</strong> и <em>курсив</em>.</p>")
        self.assertEqual(render_markup("- а\n- б"), "<ul><li>а</li><li>б</li></ul>")
        self.assertEqual(render_markup("1. а\n2) б"), "<ol><li>а</li><li>б</li></ol>")
        self.assertEqual(render_markup("## Заголовок\n\nТекст"), "<p><strong>Заголовок</strong></p><p>Текст</p>")
        self.assertEqual(render_markup("Строка один\nстрока два"), "<p>Строка один<br>строка два</p>")
        self.assertEqual(render_markup("[сайт](http://evil.test) и `код`"), "<p>сайт и код</p>")
        self.assertEqual(render_markup("до\n\n---\n\nпосле"), "<p>до</p><p>после</p>")
        self.assertEqual(render_markup("Незакрытое **жирное"), "<p>Незакрытое **жирное</p>")

    def test_html_from_an_answer_is_never_drawn_as_html(self):
        drawn = render_markup("<script>alert(1)</script> <img src=x onerror=y> & **ok**")
        self.assertNotIn("<script", drawn)
        self.assertNotIn("<img", drawn)
        self.assertIn("&lt;script&gt;", drawn)
        self.assertIn("<strong>ok</strong>", drawn)

    def test_the_blind_shuffle_is_seeded_and_mixes_sides(self):
        ids = ["Q%d" % i for i in range(12)]
        first = assign_sides(ids, "C0", "C1", "seed")
        self.assertEqual(first, assign_sides(ids, "C0", "C1", "seed"))
        self.assertNotEqual(first, assign_sides(ids, "C0", "C1", "another seed"))
        sides = {s["1"] for s in first.values()}
        self.assertEqual(sides, {"C0", "C1"})
        self.assertTrue(all(sorted(s.values()) == ["C0", "C1"] for s in first.values()))

    def test_both_answers_use_the_same_renderer_and_the_page_has_no_scripts_from_answers(self):
        card = {"id": "N01", "chart_label": "Карта", "question": "Вопрос?", "answers": [
            {"response": "**А**\n\n- один", "history": []},
            {"response": "<script>x</script>", "history": [{"question": "q", "answer": "a"}]}]}
        page = build_page([card], "Заголовок")
        self.assertEqual(page.count("<script>"), 1)                       # only the page's own copy-button script
        self.assertIn("<strong>А</strong>", page)
        self.assertIn("&lt;script&gt;x&lt;/script&gt;", page)


class JudgeTests(unittest.TestCase):
    def left_right(self):
        base = {"q": "N01", "run": 1, "question": "Какие у меня сильные стороны?", "subject": "natal", "chart": "A",
                "conv": "A", "seeded_history": False, "history": []}
        return {**base, "config": "C0", "response": "Ответ ноль"}, {**base, "config": "C1", "response": "Ответ один"}

    def metered(self, behaviour, tmp):
        return MeteredClient(FakeInner(behaviour), Budget(5.0, Path(tmp) / "s.jsonl"), JsonlWriter(Path(tmp) / "c.jsonl"), config="judge")

    def test_schema_reply_maps_the_verdict_back_to_configurations_in_both_orders(self):
        left, right = self.left_right()
        facts = {"natal": {"A": {"placements": []}}}
        reply = response(1000, 200, model="gpt-6.1-sol")
        reply.choices[0].message.content = json.dumps(judgment("2"))
        with tempfile.TemporaryDirectory() as tmp:
            metered = self.metered(lambda: reply, tmp)
            forward = judge_one(metered, left, right, "XY", facts)
            backward = judge_one(metered, left, right, "YX", facts)
        self.assertEqual((forward["outcome"], forward["winner"]), ("ok", "C1"))      # answer 2 is C1 in the first order
        self.assertEqual((backward["outcome"], backward["winner"]), ("ok", "C0"))    # and C0 in the swapped order
        self.assertEqual(pair_verdict([forward, backward]), "tie")                  # the two orders disagree: a tie

    def test_the_judge_sees_the_answers_swapped_and_no_configuration_label(self):
        left, right = self.left_right()
        payload = json.loads(build_messages(right, left, {"natal": {"A": {"placements": []}}})[1]["content"])
        self.assertEqual((payload["answer_1"], payload["answer_2"]), ("Ответ один", "Ответ ноль"))
        self.assertNotIn("C0", json.dumps(payload))
        self.assertNotIn("C1", json.dumps(payload))

    def test_a_reply_that_breaks_the_schema_is_invalid_output_and_is_not_retried(self):
        left, right = self.left_right()
        reply = response(1000, 200, model="gpt-6.1-sol")
        reply.choices[0].message.content = json.dumps({"answer": "x", "follow_up_suggestions": []})
        with tempfile.TemporaryDirectory() as tmp:
            inner = FakeInner(lambda: reply)
            metered = MeteredClient(inner, Budget(5.0, Path(tmp) / "s.jsonl"), JsonlWriter(Path(tmp) / "c.jsonl"), config="judge")
            record = judge_one(metered, left, right, "XY", {"natal": {"A": {}}})
            self.assertEqual(record["outcome"], "invalid_output")
            self.assertEqual(inner.calls, 1)

    def test_parse_judgment_is_strict(self):
        good = judgment("tie")
        self.assertIsNotNone(parse_judgment(json.dumps(good)))
        for broken in (
                {**good, "extra": 1},
                {**good, "winner": "3"},
                {**good, "answer_1": {**good["answer_1"], "scores": {**good["answer_1"]["scores"], "facts": 6}}},
                {**good, "answer_1": {**good["answer_1"], "scores": {**good["answer_1"]["scores"], "facts": "4"}}},
                {**good, "answer_2": {"scores": good["answer_2"]["scores"], "fact_errors": []}}):
            self.assertIsNone(parse_judgment(json.dumps(broken)), broken)
        self.assertIsNone(parse_judgment("not json"))
        self.assertIsNone(parse_judgment(None))

    def test_a_win_needs_both_orders_and_resolved_pairs_drive_the_threshold(self):
        def j(q, order, winner, left_score=3, right_score=4):
            return {"q": q, "run": 1, "left": "C0", "right": "C1", "order": order, "outcome": "ok", "winner": winner,
                    "reason": "r", "scores": {"C0": {c: left_score for c in ("specific", "facts", "language", "structure", "usefulness", "no_filler")},
                                                "C1": {c: right_score for c in ("specific", "facts", "language", "structure", "usefulness", "no_filler")}},
                    "fact_errors": {"C0": [], "C1": []}, "boundary_kept": {"C0": "n/a", "C1": "n/a"}}
        answers = [{"config": c, "q": q, "run": 1, "outcome": "ok", "response": "x"} for c in ("C0", "C1") for q in ("N01", "N03", "N04")]
        judgments = [j("N01", "XY", "C1"), j("N01", "YX", "C1"),          # both agree: C1 wins
                     j("N03", "XY", "C1"), j("N03", "YX", "C0"),          # disagree: tie
                     j("N04", "XY", "C0"), {**j("N04", "YX", "C0"), "outcome": "invalid_output"}]      # one invalid
        self.assertEqual(pair_verdict([judgments[0], judgments[1]]), "C1")
        self.assertEqual(pair_verdict([judgments[2], judgments[3]]), "tie")
        self.assertEqual(pair_verdict([judgments[4], judgments[5]]), "invalid")
        summary = judge_metrics(answers, judgments, "C0", "C1")
        self.assertEqual((summary["resolved"], summary["right_win_share"], summary["verdicts"]),
                         (1, 1.0, {"C1": 1, "tie": 1, "invalid": 1}))
        self.assertEqual(summary["criteria_diff"]["facts"], 1.0)
        rows = {title: result for title, _, result in thresholds({}, summary)}
        self.assertEqual(rows["Судья: C1 выигрывает >= 65% решённых пар при >= 25 решённых"], "unclear")


if __name__ == "__main__":
    unittest.main()
