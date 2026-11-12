"""CHAT-QUALITY-eval, round 2 (review EVAL-TOOL-REVIEW.md): the money guards cannot be bypassed by a path, a number or
an inherited environment variable; plus the small honesty fixes (params-func, probe folder, best model, re-run)."""
import json
import os
import subprocess
import sys
import tempfile
import unittest

import chat_eval_secrets_guard  # noqa: F401  (no test of this run may open .secrets; tests/chat_eval_secrets_guard.py)
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from chat_eval_fake_server import FakeOpenAI
from test_chat_eval_budget import REPO, run_cli
from tools.chat_eval import cli, common
from tools.chat_eval.budget import Budget
from tools.chat_eval.page import render_markup  # noqa: F401  (import check of the page module)
from tools.chat_eval.report import build_pages


def key_file(root):
    path = Path(root) / "openai-eval.env"
    path.write_text("OPENAI_API_KEY=sk-test-%s\n" % uuid4().hex, encoding="utf-8")
    return path


def lines(path):
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]


class SharedPaidStateTests(unittest.TestCase):
    """P1-1: in paid mode the money ledger and run.lock live in ONE place, whatever --run-dir is."""

    def test_the_paid_ledger_does_not_depend_on_the_run_dir(self):
        places = set()
        for run_dir in ("C:/x/eval/20261113-1", "C:/x/eval/20261113-1/probe", "C:/x/CHAT-QUALITY/eval-real", "D:/elsewhere/run"):
            _, ledger = cli.settle_paths(Namespace(real=True, run_dir=run_dir, ledger_dir=""))
            places.add(str(ledger))
        self.assertEqual(places, {str(common.paid_state_dir() / "spend.jsonl")})
        self.assertEqual(common.paid_state_dir().name, "eval")
        self.assertEqual(common.paid_state_dir().parent.name, "CHAT-QUALITY")

    def test_the_dry_ledger_stays_inside_its_run_dir(self):
        _, ledger = cli.settle_paths(Namespace(real=False, run_dir="C:/x/dry", ledger_dir=""))
        self.assertEqual(Path(ledger), Path("C:/x/dry").resolve() / "spend.jsonl")

    def test_two_paid_runs_under_different_parents_share_one_limit(self):
        with tempfile.TemporaryDirectory() as tmp, FakeOpenAI(usage=(10000, 1000)) as server:     # about $0.0021 a call
            kf, shared = key_file(tmp), Path(tmp) / "shared"
            base = ["run", "--real", "--real-base-url", server.url, "--key-file", str(kf), "--ledger-dir", str(shared),
                    "--configs", "C0", "--runs", "1", "--only", "N01,N02,N03,N04,N05,N06,N07", "--budget-usd", "0.009",
                    "--base-repo", str(REPO), "--candidate-repo", str(REPO)]
            first = run_cli(*base, "--run-dir", str(Path(tmp) / "a" / "run"))
            made = server.count("generation")
            second = run_cli(*base, "--run-dir", str(Path(tmp) / "b" / "run"))
            self.assertEqual((first.returncode, second.returncode), (3, 3), first.stdout[-500:] + second.stdout[-500:])
            self.assertEqual(server.count("generation"), made)               # the second run was refused before any call
            self.assertGreater(made, 0)
            self.assertEqual(lines(Path(tmp) / "b" / "run" / "answers.jsonl")[0]["outcome"], "budget_stop")
            self.assertTrue((shared / "spend.jsonl").is_file())
            self.assertFalse((Path(tmp) / "a" / "spend.jsonl").exists())
            self.assertLessEqual(Budget(8.5, shared / "spend.jsonl").spent(), 0.009)

    def test_a_ledger_dir_override_is_only_for_a_loopback_rehearsal(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = run_cli("run", "--real", "--key-file", str(key_file(tmp)), "--ledger-dir", tmp, "--configs", "C0",
                             "--base-repo", str(REPO), "--candidate-repo", str(REPO), "--run-dir", str(Path(tmp) / "run"))
            dry = run_cli("run", "--ledger-dir", tmp, "--configs", "C0", "--base-repo", str(REPO),
                          "--candidate-repo", str(REPO), "--run-dir", str(Path(tmp) / "run2"))
        for outcome in (result, dry):
            self.assertEqual(outcome.returncode, 2)
            self.assertIn("reason=ledger_dir_only_for_rehearsal", outcome.stdout)


class BudgetValueTests(unittest.TestCase):
    """P2-1: the limit is a finite number, > 0 and <= 8.5 (DECISIONS 17: below the owner OpenAI project limit of 9, which is itself NOT
    allowed), checked by the CLI, the worker and the ledger itself."""
    BAD = ("nan", "inf", "-inf", "-1", "0", "8.51", "9", "9.01", "10.01", "1e9")

    def test_the_cli_refuses_every_bad_limit_before_any_call(self):
        with FakeOpenAI() as server, tempfile.TemporaryDirectory() as tmp:
            for value in self.BAD:
                result = run_cli("run", "--configs", "C0", "--runs", "1", "--only", "N01", "--base-repo", str(REPO),
                                 "--candidate-repo", str(REPO), "--run-dir", str(Path(tmp) / "run"), "--base-url", server.url,
                                 "--budget-usd=" + value)
                self.assertEqual(result.returncode, 2, value)
                self.assertIn("reason=budget_out_of_range", result.stdout, value)
            self.assertEqual(server.count(), 0)

    def test_the_worker_run_directly_refuses_them_too(self):
        with FakeOpenAI() as server, tempfile.TemporaryDirectory() as tmp:
            for value in self.BAD:
                env = {k: v for k, v in os.environ.items() if not k.upper().startswith("OPENAI_")}
                env["PYTHONUTF8"] = "1"
                result = subprocess.run(
                    [sys.executable, "-m", "tools.chat_eval.worker", "--repo", str(REPO), "--config", "C0", "--out-dir",
                     str(Path(tmp) / "out"), "--ledger", str(Path(tmp) / "l.jsonl"), "--budget-usd=" + value, "--base-url",
                     server.url, "--runs", "1", "--only", "N01"], cwd=str(REPO), env=env, capture_output=True, text=True,
                    encoding="utf-8", timeout=120)
                self.assertEqual(result.returncode, 2, value + result.stdout[-300:])
                self.assertIn("reason=budget_out_of_range", result.stdout, value)
            self.assertEqual(server.count(), 0)

    def test_the_ledger_object_rejects_them(self):
        with tempfile.TemporaryDirectory() as tmp:
            for value in (float("nan"), float("inf"), -1.0, 0.0, 8.51, 9.0, 9.01, 10.01):
                with self.assertRaises(ValueError, msg=str(value)):
                    Budget(value, Path(tmp) / "l.jsonl")
            Budget(8.5, Path(tmp) / "l.jsonl")


class ProviderAddressTests(unittest.TestCase):
    """P2-2: in paid mode every process uses the explicit official address; an inherited OPENAI_BASE_URL is dropped."""

    def paid_args(self, tmp, **extra):
        return Namespace(real=True, run_dir=str(Path(tmp) / "run"), ledger_dir="", real_base_url="", budget_usd=8.5,
                         key_file=str(key_file(tmp)), base_url=common.STAND_OPENAI_URL, **extra)

    def test_prepare_removes_the_inherited_base_url_in_paid_mode(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"OPENAI_BASE_URL": "https://proxy.example.invalid/v1"}):
            self.addCleanup(common._SECRETS.clear)
            cli.prepare(self.paid_args(tmp))
            self.assertFalse("OPENAI_BASE_URL" in os.environ)   # not assertNotIn(os.environ): a failure would print the environment

    def test_the_paid_judge_client_points_at_the_official_address_whatever_the_environment_says(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"OPENAI_BASE_URL": "https://proxy.example.invalid/v1",
                                                                          "OPENAI_API_KEY": "sk-test-not-used-anywhere"}):
            metered = cli.make_metered(self.paid_args(tmp), Path(tmp) / "run", Path(tmp) / "spend.jsonl", "judge")
            self.assertEqual(str(metered.inner.base_url), "https://api.openai.com/v1/")

    def test_the_address_rule_is_one_function_for_every_process(self):
        self.assertEqual(common.client_base_url(real=True, dry_url="http://127.0.0.1:1/v1", real_base_url=""),
                         "https://api.openai.com/v1")
        self.assertEqual(common.client_base_url(real=True, dry_url="http://127.0.0.1:1/v1", real_base_url="http://127.0.0.1:5/v1"),
                         "http://127.0.0.1:5/v1")
        self.assertEqual(common.client_base_url(real=False, dry_url="http://127.0.0.1:1/v1", real_base_url=""),
                         "http://127.0.0.1:1/v1")


class SmallHonestyTests(unittest.TestCase):
    def test_params_func_takes_the_named_module(self):
        from tools.chat_eval.worker import product_params
        params, source = product_params(SimpleNamespaceEmpty(), "chat_eval_fake_server:PARAMS_FOR_TEST")
        self.assertEqual(params, {"model": "gpt-4o-mini", "temperature": 0.1})
        self.assertEqual(source, "chat_eval_fake_server:PARAMS_FOR_TEST")
        self.assertEqual(product_params(SimpleNamespaceEmpty(), "no_such_module_here:X"), (None, None))
        self.assertEqual(product_params(SimpleNamespaceEmpty(), "just_a_name"), (None, None))

    def test_the_probe_writes_into_a_probe_folder_of_the_run_dir(self):
        with FakeOpenAI() as server, tempfile.TemporaryDirectory() as tmp:
            result = run_cli("probe", "--configs", "P0,P1,P2", "--base-repo", str(REPO), "--v3-repo", str(REPO),
                             "--run-dir", str(Path(tmp) / "run"), "--base-url", server.url)
            self.assertEqual(result.returncode, 0, result.stdout[-600:])
            self.assertEqual({a["config"] for a in lines(Path(tmp) / "run" / "probe" / "answers.jsonl")}, {"P0", "P1", "P2"})
            self.assertFalse((Path(tmp) / "run" / "answers.jsonl").exists())

    def test_a_second_run_on_a_folder_with_answers_is_refused_without_spending(self):
        with FakeOpenAI() as server, tempfile.TemporaryDirectory() as tmp:
            args = ["run", "--configs", "C0", "--runs", "1", "--only", "N01", "--base-repo", str(REPO),
                    "--candidate-repo", str(REPO), "--run-dir", str(Path(tmp) / "run"), "--base-url", server.url]
            self.assertEqual(run_cli(*args).returncode, 0)
            made = server.count()
            again = run_cli(*args)
            self.assertEqual(again.returncode, 2)
            self.assertIn("reason=answers_already_exist", again.stdout)
            self.assertEqual(server.count(), made)
            self.assertEqual(run_cli(*args, "--allow-existing").returncode, 0)          # only on an explicit flag
            self.assertEqual(server.count(), made * 2)

    def test_the_model_page_uses_the_better_of_c2_and_c3_by_the_judge_and_says_so(self):
        entry = {"placements": [["Солнце", "Рыбы", "23°05′", 12, False]]}
        facts = {"natal": {"A": entry}, "pairs": {}}
        base = {"q": "N01", "run": 1, "outcome": "ok", "question": "Вопрос?", "subject": "natal", "chart": "A", "conv": "A", "history": []}
        answers = [{**base, "config": c, "response": "ответ " + c} for c in ("C1", "C2", "C3")]

        def verdicts(winner):
            return [{"q": "N01", "run": 1, "left": "C2", "right": "C3", "order": o, "outcome": "ok", "winner": winner}
                    for o in ("XY", "YX")]
        for winner, expected in (("C3", "C3"), ("C2", "C2")):
            with tempfile.TemporaryDirectory() as tmp:
                build_pages(tmp, answers, facts, "seed", verdicts(winner))
                key = json.loads((Path(tmp) / "key.json").read_text(encoding="utf-8"))
                self.assertEqual(sorted(key["pages"]["compare-models.html"]["N01"].values()), sorted(["C1", expected]), winner)
                self.assertEqual(key["model_page_second"], expected)
        with tempfile.TemporaryDirectory() as tmp:                                       # no judge data: C2 and it is said
            build_pages(tmp, answers, facts, "seed", [])
            key = json.loads((Path(tmp) / "key.json").read_text(encoding="utf-8"))
            self.assertEqual((key["model_page_second"], key["model_page_reason"]), ("C2", "no_judge_data"))


class SimpleNamespaceEmpty:
    """Stands for modules.interpretation with none of the usual parameter names."""


if __name__ == "__main__":
    unittest.main()
