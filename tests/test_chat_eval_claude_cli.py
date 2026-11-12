"""CHAT-CLAUDE step 3, E-5 and the command line: the new configurations, the refusal to start when the product does not match
a configuration, the staged judging (controls -> effort -> main) with both judges, the per-provider caps in a paid rehearsal
against loopback fakes, and the dry cycle (the shape of E-11 without the product's Anthropic path).

Every provider here is a loopback fake. The product checkout is this worktree itself: it has no Anthropic path, which is exactly
what the refusal tests need; the Anthropic path of the product is exercised later, on the stand, by the coordinator (E-11).
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

import chat_eval_secrets_guard  # noqa: F401  (no test of this run may open .secrets; tests/chat_eval_secrets_guard.py)
from argparse import Namespace
from pathlib import Path
from uuid import uuid4

from chat_eval_fake_server import FakeAnthropic, FakeOpenAI
from test_chat_eval_budget import REPO, run_cli
from test_chat_eval_claude_page import CONFIGS as ALL_CONFIGS, fabricate
from tools.chat_eval import budget, cli, common, questions as Q


class _Lazy:
    def __getattr__(self, attr):
        import importlib
        return getattr(importlib.import_module("tools.chat_eval.controls"), attr)


controls = _Lazy()

EXPECTED = {
    "C0": ("base", "openai", "gpt-4o-mini", None),
    "G2": ("v2", "openai", "gpt-6-luna", "none"), "G3": ("v3", "openai", "gpt-6-luna", "none"), "G3x": ("v3", "openai", "gpt-6-luna", "low"),
    "H2": ("v2", "anthropic", "claude-haiku-5-5", "low"), "H3": ("v3", "anthropic", "claude-haiku-5-5", "low"),
    "H3x": ("v3", "anthropic", "claude-haiku-5-5", "medium"),
    "P1": ("v3", "openai", "gpt-6-luna", "none"), "P2": ("v3", "openai", "gpt-6-luna", "low"),
    "PH1": ("v3", "anthropic", "claude-haiku-5-5", "low"), "PH2": ("v3", "anthropic", "claude-haiku-5-5", "medium"),
}


def lines(path):
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]


def worker(*args, env_extra=None):
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith(("OPENAI_", "ANTHROPIC_"))}
    env.update(PYTHONUTF8="1", **(env_extra or {}))
    return subprocess.run([sys.executable, "-m", "tools.chat_eval.worker", *args], cwd=str(REPO), env=env, capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=300)


def key_file(root, name="anthropic-eval.env", var="ANTHROPIC_API_KEY", prefix="sk-ant-test-"):
    path = Path(root) / name
    path.write_text("%s=%s%s\n" % (var, prefix, uuid4().hex), encoding="utf-8")
    return path


class ConfigurationTests(unittest.TestCase):
    """E-5."""

    def test_the_design_table(self):
        for name, (code, provider, model, effort) in EXPECTED.items():
            entry = common.CONFIGS[name]
            self.assertEqual((entry["code"], entry["provider"], entry["model"], entry["effort"]), (code, provider, model, effort), name)

    def test_every_configuration_has_a_price_and_a_provider_that_matches_its_model(self):
        for name, entry in common.CONFIGS.items():
            budget.price_for(entry["model"])
            self.assertEqual(common.provider_of(entry["model"]), entry["provider"], name)

    def test_the_cheap_effort_is_the_default_and_the_costly_one_is_a_separate_configuration(self):
        self.assertEqual((common.CONFIGS["G3"]["effort"], common.CONFIGS["H3"]["effort"]), ("none", "low"))
        self.assertEqual((common.CONFIGS["G3x"]["effort"], common.CONFIGS["H3x"]["effort"]), ("low", "medium"))
        for same in (("G2", "G3"), ("H2", "H3")):                      # only the prompt differs inside a family
            a, b = common.CONFIGS[same[0]], common.CONFIGS[same[1]]
            self.assertEqual((a["provider"], a["model"], a["effort"]), (b["provider"], b["model"], b["effort"]))
        a, b = common.CONFIGS["G3"], common.CONFIGS["H3"]                # only the model differs between the families
        self.assertEqual(a["code"], b["code"])

    def test_the_checkout_of_a_configuration_follows_its_code_and_a_missing_one_is_refused(self):
        args = Namespace(base_repo="b", v2_repo="v2", v3_repo="v3", candidate_repo="c")
        self.assertEqual([cli.repo_for(args, c) for c in ("C0", "G2", "H2", "G3", "H3x", "C1")], ["b", "v2", "v2", "v3", "v3", "c"])
        for config, missing in (("G2", "v2_repo"), ("H3", "v3_repo")):
            args = Namespace(base_repo="b", v2_repo="v2", v3_repo="v3", candidate_repo="c")
            setattr(args, missing, None)
            with self.assertRaises(SystemExit) as caught:
                cli.repo_for(args, config)
            self.assertEqual(caught.exception.code, 2)

    def test_the_probes_are_one_question_each_for_both_efforts_of_both_families(self):
        self.assertEqual(list(cli.PROBE_CONFIGS), ["P1", "P2", "PH1", "PH2"])


class WorkerRefusalTests(unittest.TestCase):
    """The worker refuses before the first call when the product does not match the configuration."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def args(self, config, url, *extra, budget_usd="5"):
        return ["--repo", str(REPO), "--config", config, "--out-dir", str(self.dir / "out"), "--ledger", str(self.dir / "spend.jsonl"),
                "--budget-usd", budget_usd, "--base-url", url, "--runs", "1", "--only", "N01", *extra]

    def test_an_anthropic_configuration_on_a_product_without_the_provider_is_refused_without_a_call(self):
        with FakeAnthropic() as server:
            result = worker(*self.args("H3", server.url))
            self.assertEqual(server.count(), 0)
        self.assertEqual(result.returncode, 2, result.stdout[-500:])
        self.assertIn("outcome=refused reason=product_params_unavailable config=H3", result.stdout)
        self.assertFalse((self.dir / "out" / "answers.jsonl").exists())

    def test_the_paid_run_with_the_cap_unset_is_refused_before_anything_else(self):
        with FakeAnthropic() as server:
            result = worker(*self.args("H3", "http://127.0.0.1:9", "--real", "--real-base-url", server.url, budget_usd="0"),
                            env_extra={"ANTHROPIC_API_KEY": "sk-ant-test-" + "k" * 30})
            self.assertEqual(server.count(), 0)
        self.assertEqual(result.returncode, 2, result.stdout[-500:])
        self.assertIn("reason=anthropic_budget_unset", result.stdout)

    def test_the_paid_run_needs_its_own_providers_key_and_never_uses_the_stub(self):
        with FakeAnthropic() as server:
            none = worker(*self.args("H3", "http://127.0.0.1:9", "--real", "--real-base-url", server.url))
            stub = worker(*self.args("H3", "http://127.0.0.1:9", "--real", "--real-base-url", server.url),
                          env_extra={"ANTHROPIC_API_KEY": common.STUB_ANTHROPIC_KEY})
            only_openai = worker(*self.args("H3", "http://127.0.0.1:9", "--real", "--real-base-url", server.url),
                                 env_extra={"OPENAI_API_KEY": "sk-openai-" + "k" * 30})
            self.assertEqual(server.count(), 0)
        for result in (none, stub, only_openai):
            self.assertEqual(result.returncode, 2, result.stdout[-500:])
            self.assertIn("reason=no_key", result.stdout)

    def test_the_dry_run_of_an_anthropic_configuration_needs_a_loopback_address(self):
        result = worker(*self.args("H3", "https://api.anthropic.com"))
        self.assertEqual(result.returncode, 2)
        self.assertIn("reason=dry_needs_loopback_url", result.stdout)

    def test_the_paid_anthropic_run_must_use_the_shared_ledger_unless_it_is_a_loopback_rehearsal(self):
        result = worker(*self.args("H3", "http://127.0.0.1:9", "--real"), env_extra={"ANTHROPIC_API_KEY": "sk-ant-test-" + "k" * 30})
        self.assertEqual(result.returncode, 2)
        self.assertIn("reason=paid_ledger_must_be_the_shared_one", result.stdout)

    def test_a_cap_above_the_owners_credits_is_out_of_range_for_anthropic_and_for_openai_above_nine(self):
        with FakeAnthropic() as anthropic_server, FakeOpenAI() as openai_server:
            a = worker(*self.args("H3", anthropic_server.url, budget_usd="10.01"))
            b = worker(*self.args("G3", openai_server.url, budget_usd="9.01"))
            self.assertEqual((anthropic_server.count(), openai_server.count()), (0, 0))
        for result in (a, b):
            self.assertEqual(result.returncode, 2)
            self.assertIn("reason=budget_out_of_range", result.stdout)

    def test_an_openai_configuration_still_runs_the_product_path_with_its_new_name_and_records_its_family(self):
        with FakeOpenAI() as server:
            result = worker(*self.args("G3", server.url))
            made = server.count("generation")
        self.assertEqual(result.returncode, 0, result.stdout[-800:])
        self.assertEqual(made, 1)
        answer = lines(self.dir / "out" / "answers.jsonl")[0]
        self.assertEqual((answer["config"], answer["model_requested"], answer["outcome"]), ("G3", "gpt-6-luna", "ok"))
        meta = json.loads((self.dir / "out" / "meta-G3.json").read_text(encoding="utf-8"))
        self.assertEqual((meta["code"], meta["effort"], meta["provider"]), ("v3", "none", "openai"))


class RunCommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_the_new_configurations_run_from_their_own_checkouts_and_a_missing_checkout_is_refused(self):
        with FakeOpenAI() as server:
            ok = run_cli("run", "--configs", "G2", "--runs", "1", "--only", "N01", "--v2-repo", str(REPO), "--run-dir", str(self.root / "ok"),
                         "--base-url", server.url)
            made = server.count()
            missing = run_cli("run", "--configs", "G2", "--runs", "1", "--only", "N01", "--run-dir", str(self.root / "no"), "--base-url", server.url)
            self.assertEqual(server.count(), made)
        self.assertEqual(ok.returncode, 0, ok.stdout[-800:])
        self.assertEqual(lines(self.root / "ok" / "answers.jsonl")[0]["config"], "G2")
        self.assertEqual(missing.returncode, 2)
        self.assertIn("reason=missing_v2_repo", missing.stdout)

    def test_an_anthropic_configuration_on_the_old_product_stops_the_run_and_calls_nobody(self):
        with FakeOpenAI() as openai_server, FakeAnthropic() as anthropic_server:
            result = run_cli("run", "--configs", "H2", "--runs", "1", "--only", "N01", "--v2-repo", str(REPO), "--run-dir", str(self.root / "r"),
                             "--base-url", openai_server.url, "--anthropic-base-url", anthropic_server.url)
            self.assertEqual((openai_server.count(), anthropic_server.count()), (0, 0))
        self.assertEqual(result.returncode, 2, result.stdout[-600:])
        self.assertIn("outcome=worker_failed config=H2 exit_code=2", result.stdout)

    def test_a_paid_anthropic_run_with_the_cap_at_zero_is_refused_before_the_key_file_is_even_looked_for(self):
        result = run_cli("run", "--real", "--configs", "H2", "--v2-repo", str(REPO), "--anthropic-budget-usd", "0",
                         "--anthropic-key-file", str(self.root / "nope.env"), "--run-dir", str(self.root / "r"))
        self.assertEqual(result.returncode, 2)
        self.assertIn("reason=anthropic_budget_unset", result.stdout)
        self.assertNotIn("key_file_missing", result.stdout)

    def test_an_openai_only_paid_command_does_not_need_the_anthropic_key_or_cap(self):
        with FakeOpenAI() as server:
            result = run_cli("run", "--real", "--real-base-url", server.url, "--configs", "G2", "--runs", "1", "--only", "N01",
                             "--v2-repo", str(REPO), "--key-file", str(key_file(self.root, "openai-eval.env", "OPENAI_API_KEY", "sk-test-")),
                             "--ledger-dir", str(self.root / "ledger"), "--anthropic-budget-usd", "0", "--run-dir", str(self.root / "r"))
        self.assertEqual(result.returncode, 0, result.stdout[-600:])
        self.assertEqual(budget.spent_by_provider(self.root / "ledger" / "spend.jsonl")["anthropic"], 0)
        self.assertEqual({r["provider"] for r in lines(self.root / "ledger" / "spend.jsonl")}, {"openai"})

    def test_the_probe_of_the_openai_family_writes_into_its_own_folder(self):
        with FakeOpenAI() as server:
            result = run_cli("probe", "--configs", "P1,P2", "--v3-repo", str(REPO), "--run-dir", str(self.root / "run"), "--base-url", server.url)
        self.assertEqual(result.returncode, 0, result.stdout[-600:])
        self.assertEqual({a["config"] for a in lines(self.root / "run" / "probe" / "answers.jsonl")}, {"P1", "P2"})

    def test_the_default_probe_includes_the_anthropic_efforts_and_stops_on_the_old_product(self):
        with FakeOpenAI() as openai_server, FakeAnthropic() as anthropic_server:
            result = run_cli("probe", "--v3-repo", str(REPO), "--run-dir", str(self.root / "run"), "--base-url", openai_server.url,
                             "--anthropic-base-url", anthropic_server.url)
            self.assertEqual(anthropic_server.count(), 0)
        self.assertEqual(result.returncode, 2)
        self.assertIn("config=PH1", result.stdout)

    def test_a_cycle_is_dry_only(self):
        result = run_cli("cycle", "--claude", "--real", "--run-dir", str(self.root / "r"), "--configs", "C0")
        self.assertEqual(result.returncode, 2)
        self.assertIn("reason=cycle_is_dry_only", result.stdout)


class JudgeStageTests(unittest.TestCase):
    """judge --stage controls | effort | main with both judges. The fakes answer judge prompts with a foreign envelope unless told otherwise."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.run_dir = self.root / "run"

    def judge(self, stage, openai_server, anthropic_server, *extra):
        return run_cli("judge", "--stage", stage, "--run-dir", str(self.run_dir), "--base-url", openai_server.url,
                       "--anthropic-base-url", anthropic_server.url, "--seed", "s", *extra)

    def test_controls_send_16_calls_per_judge_and_a_judge_that_fails_them_stops_the_run(self):
        with FakeOpenAI() as openai_server, FakeAnthropic() as anthropic_server:
            result = self.judge("controls", openai_server, anthropic_server)
            self.assertEqual(anthropic_server.count("judge"), 12)                  # 6 pairs x 2 orders
            self.assertEqual(anthropic_server.count("attribution"), 4)             # 2 controls x 2 orders
            self.assertEqual(openai_server.count(), 16)
        self.assertEqual(result.returncode, 5, result.stdout[-800:])
        self.assertIn("chat_eval.judge stage=controls outcome=j0_failed", result.stdout)
        verdict = json.loads((self.run_dir / "controls.json").read_text(encoding="utf-8"))
        self.assertEqual({k: v["passed"] for k, v in verdict.items()}, {"openai": False, "claude": False})
        self.assertEqual(len(lines(self.run_dir / "judgments-controls.jsonl")), 24)        # 12 pair judgments of each judge
        self.assertEqual(len(lines(self.run_dir / "attribution-controls.jsonl")), 8)
        self.assertEqual({j["judge"] for j in lines(self.run_dir / "judgments-controls.jsonl")}, {"openai", "claude"})
        self.assertFalse((self.run_dir / "answers.jsonl").exists())                         # the controls need no model answers

    def test_a_single_judge_can_be_chosen(self):
        with FakeOpenAI() as openai_server, FakeAnthropic() as anthropic_server:
            self.judge("controls", openai_server, anthropic_server, "--judges", "claude")
            self.assertEqual(openai_server.count(), 0)
            self.assertEqual(anthropic_server.count(), 16)

    def test_the_controls_are_blind_and_use_the_designed_questions(self):
        with FakeOpenAI() as openai_server, FakeAnthropic() as anthropic_server:
            self.judge("controls", openai_server, anthropic_server, "--judges", "claude")
            bodies = [b for b in anthropic_server.bodies if "blind evaluator" in json.dumps(b["system"])]
        asked = {json.loads(b["messages"][0]["content"][-1]["text"])["question"] for b in bodies}
        self.assertEqual(asked, {Q.question_text(c["q"]) for c in controls.CONTROL_PAIRS})
        for body in bodies:
            self.assertNotIn('"bad"', json.dumps(body, ensure_ascii=False))
            self.assertNotIn('"good"', json.dumps(body, ensure_ascii=False))

    def test_the_claude_judge_model_is_a_named_choice_with_a_default(self):
        self.assertEqual(common.ANTHROPIC_JUDGE_MODEL, "claude-opus-5-5")
        with FakeOpenAI() as openai_server, FakeAnthropic() as anthropic_server:
            self.judge("controls", openai_server, anthropic_server, "--judges", "claude")
            self.assertEqual({b["model"] for b in anthropic_server.bodies}, {"claude-opus-5-5"})
        self.setUp()
        with FakeOpenAI() as openai_server, FakeAnthropic() as anthropic_server:
            self.judge("controls", openai_server, anthropic_server, "--judges", "claude", "--claude-judge-model", "claude-sonnet-5-5")
            self.assertEqual({b["model"] for b in anthropic_server.bodies}, {"claude-sonnet-5-5"})

    def test_a_stage_without_answers_is_refused_and_so_is_an_unknown_stage(self):
        with FakeOpenAI() as openai_server, FakeAnthropic() as anthropic_server:
            result = self.judge("effort", openai_server, anthropic_server)
            self.assertEqual((result.returncode, openai_server.count(), anthropic_server.count()), (2, 0, 0))
            self.assertIn("reason=no_answers_to_judge", result.stdout)
            bad = self.judge("nonsense", openai_server, anthropic_server)
            self.assertEqual(bad.returncode, 2)

    def test_effort_judges_only_s10_by_claude_and_main_runs_j1_j2_and_j4_with_the_designed_judges(self):
        fabricate(self.run_dir, verdicts=False)
        with FakeOpenAI() as openai_server, FakeAnthropic() as anthropic_server:
            effort = self.judge("effort", openai_server, anthropic_server)
            self.assertEqual(effort.returncode, 0, effort.stdout[-800:])
            self.assertEqual(anthropic_server.count("judge"), 40)                   # 2 pairs x 10 questions x 2 orders, all by Claude
            self.assertEqual(openai_server.count(), 0)
            main = self.judge("main", openai_server, anthropic_server)
            self.assertEqual(main.returncode, 0, main.stdout[-800:])
            self.assertEqual(anthropic_server.count("judge"), 40 + 56)              # J1: 28 x 2 orders by the Claude judge
            self.assertEqual(anthropic_server.count("attribution"), 12)             # C0, G2, G* are checked by Claude: 3 x 4
            self.assertEqual(openai_server.count("judge"), 56 + 112)                # J1 by OpenAI, J2: 2 pairs x 28 x 2 by OpenAI only
            self.assertEqual(openai_server.count("generation"), 8)                  # H2 and H* attribution by OpenAI (2 x 4)
        judgments = lines(self.run_dir / "judgments.jsonl")
        self.assertEqual({(j["judge"], j["left"], j["right"]) for j in judgments if j["judge"] == "claude"},
                         {("claude", "G3", "G3x"), ("claude", "H3", "H3x"), ("claude", "G3", "H3")})
        self.assertEqual({(j["left"], j["right"]) for j in judgments if j["judge"] == "openai"},
                         {("G3", "H3"), ("G2", "G3"), ("H2", "H3")})
        self.assertEqual(len(lines(self.run_dir / "attribution.jsonl")), 20)

    def test_a_second_run_of_a_stage_does_not_repeat_a_call(self):
        fabricate(self.run_dir, verdicts=False)
        with FakeOpenAI() as openai_server, FakeAnthropic() as anthropic_server:
            self.judge("effort", openai_server, anthropic_server)
            made = anthropic_server.count()
            again = self.judge("effort", openai_server, anthropic_server)
            self.assertEqual(again.returncode, 0)
            self.assertEqual(anthropic_server.count(), made)

    def paid(self, *extra, anthropic_server, run="run"):
        return run_cli("judge", "--stage", "controls", "--judges", "claude", "--real", "--anthropic-real-base-url", anthropic_server.url,
                       "--anthropic-key-file", str(key_file(self.root)), "--ledger-dir", str(self.root / "ledger"),
                       "--run-dir", str(self.root / run), *extra)

    def test_a_paid_rehearsal_charges_the_anthropic_cap_only_and_the_cap_is_checked_before_every_call(self):
        with FakeAnthropic(usage=(1000, 2000, 0, 800)) as server:
            result = self.paid("--anthropic-budget-usd", "5", anthropic_server=server)
            made = server.count()
        self.assertEqual(result.returncode, 5, result.stdout[-600:])
        self.assertEqual(made, 16)
        ledger = self.root / "ledger" / "spend.jsonl"
        by = budget.spent_by_provider(ledger)
        self.assertEqual(by["openai"], 0)
        self.assertGreater(by["anthropic"], 0)
        self.assertLessEqual(by["anthropic"], 5)
        self.assertEqual({r["provider"] for r in lines(ledger)}, {"anthropic"})
        self.assertFalse((self.root / "ledger" / "run.lock").exists())
        with FakeAnthropic() as server:
            tiny = self.paid("--anthropic-budget-usd", "0.0001", anthropic_server=server, run="tiny")
            self.assertEqual(server.count(), 0)                                      # refused before the first call
        self.assertEqual(tiny.returncode, 3, tiny.stdout[-600:])

    def test_a_paid_rehearsal_with_the_cap_at_zero_makes_no_call(self):
        with FakeAnthropic() as server:
            result = self.paid("--anthropic-budget-usd", "0", anthropic_server=server)
            self.assertEqual(server.count(), 0)
        self.assertEqual(result.returncode, 2)
        self.assertIn("reason=anthropic_budget_unset", result.stdout)

    def test_a_provider_that_echoes_the_key_leaks_nothing_in_a_paid_rehearsal(self):
        file = key_file(self.root)
        key = file.read_text(encoding="utf-8").split("=", 1)[1].strip()
        with FakeAnthropic(expected_key=key, mode="unauthorized_echo") as server:
            result = run_cli("judge", "--stage", "controls", "--judges", "claude", "--real", "--anthropic-real-base-url", server.url,
                             "--anthropic-key-file", str(file), "--anthropic-budget-usd", "5", "--ledger-dir", str(self.root / "ledger"),
                             "--run-dir", str(self.run_dir))
            self.assertGreater(server.count(), 0)
        self.assertNotIn(key, result.stdout + result.stderr)
        for path in self.root.rglob("*"):
            if path.is_file() and path.name != "anthropic-eval.env":
                self.assertNotIn(key, path.read_text(encoding="utf-8", errors="replace"), path.name)
        self.assertEqual(budget.spent_by_provider(self.root / "ledger" / "spend.jsonl")["anthropic"], 0)     # refused requests are free


class DryCycleTests(unittest.TestCase):
    """The dry cycle end to end on what this worktree can run: C0 and the OpenAI family (the Anthropic path of the product is E-11)."""

    def test_the_cycle_runs_judges_and_reports_in_one_command_and_never_leaves_the_computer(self):
        with tempfile.TemporaryDirectory() as tmp, FakeOpenAI() as openai_server, FakeAnthropic() as anthropic_server:
            run_dir = Path(tmp) / "run"
            result = run_cli("cycle", "--claude", "--configs", "C0,G2,G3,G3x", "--only", "N01,N17,S1-A,S1-D", "--base-repo", str(REPO),
                             "--v2-repo", str(REPO), "--v3-repo", str(REPO), "--run-dir", str(run_dir), "--base-url", openai_server.url,
                             "--anthropic-base-url", anthropic_server.url, "--seed", "s")
            self.assertEqual(result.returncode, 0, result.stdout[-1500:])
            for line in result.stdout.splitlines():
                self.assertTrue(line.startswith("chat_eval."), line)
            for name in ("answers.jsonl", "judgments.jsonl", "judgments-controls.jsonl", "controls.json", "attribution.jsonl",
                         "report-claude.md", "metrics-claude.json", "spend.jsonl", "meta-G3.json", "facts.json"):
                self.assertTrue((run_dir / name).is_file(), name)
            self.assertFalse((run_dir / "compare-claude.html").exists())                     # no Anthropic answers: no page
            metrics = json.loads((run_dir / "metrics-claude.json").read_text(encoding="utf-8"))
            self.assertEqual(metrics["j0"], {"openai": False, "claude": False})              # the fakes answer judges with a foreign schema
            self.assertEqual(metrics["recommendation"]["choice"], None)
            by = budget.spent_by_provider(run_dir / "spend.jsonl")
            self.assertGreater(by["openai"], 0)
            self.assertGreater(by["anthropic"], 0)
            self.assertGreater(anthropic_server.count("judge"), 0)
            self.assertGreater(anthropic_server.count("attribution"), 0)
            self.assertEqual(anthropic_server.count("generation"), 0)                        # the Claude judge is never asked to answer
        self.assertFalse(list(Path(tmp).rglob("run.lock")))


if __name__ == "__main__":
    unittest.main()
