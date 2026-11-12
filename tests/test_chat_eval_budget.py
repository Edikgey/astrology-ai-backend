"""CHAT-QUALITY-eval: the hard budget stop, spend accounting and 'no repeated calls'. Local fake provider only."""
import json
import os
import subprocess
import sys
import tempfile
import unittest

import chat_eval_secrets_guard  # noqa: F401  (no test of this run may open .secrets; tests/chat_eval_secrets_guard.py)
from pathlib import Path
from types import SimpleNamespace

from chat_eval_fake_server import FakeOpenAI
from tools.chat_eval.budget import Budget, BudgetStop, UnknownModel, cost_usd, price_for
from tools.chat_eval.metered import JsonlWriter, MeteredClient, judge_call

REPO = Path(__file__).resolve().parent.parent


class StatusError(Exception):
    def __init__(self, status):
        super().__init__("secret provider text")
        self.status_code = status


class APITimeoutError(Exception):
    """Same class name as the SDK's: the wrapper classifies by name, never by message."""


def response(prompt=100, completion=40, finish="stop", model="gpt-4o-mini"):
    return SimpleNamespace(choices=[SimpleNamespace(finish_reason=finish, message=SimpleNamespace(content="x"))],
                           model=model, usage=SimpleNamespace(prompt_tokens=prompt, completion_tokens=completion,
                                                              total_tokens=prompt + completion))


class FakeInner:
    def __init__(self, behaviour):
        self.calls = 0
        self.behaviour = behaviour
        completions = SimpleNamespace(create=self.create)
        self.chat = SimpleNamespace(completions=completions)
        self.beta = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(stream=self.stream)))

    def create(self, **kwargs):
        self.calls += 1
        result = self.behaviour()
        if isinstance(result, BaseException):
            raise result
        return result

    def stream(self, **kwargs):
        self.calls += 1
        return FakeStream(self.behaviour())


class FakeStream:
    def __init__(self, final):
        self.final = final

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __iter__(self):
        yield SimpleNamespace(type="content.delta")

    def get_final_completion(self):
        if isinstance(self.final, BaseException):
            raise self.final
        return self.final


def messages(size):
    return [{"role": "user", "content": "x" * size}]


class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ledger = Path(self.tmp.name) / "spend.jsonl"

    def test_prices_follow_the_design_table(self):
        self.assertAlmostEqual(cost_usd("gpt-4o-mini", 1_000_000, 1_000_000), 0.75)
        self.assertAlmostEqual(cost_usd("gpt-6-luna", 1_000_000, 1_000_000), 0.60)
        self.assertAlmostEqual(cost_usd("gpt-6.1-sol", 1_000_000, 1_000_000), 12.0)
        self.assertEqual(price_for("gpt-4o-mini-2024-07-18"), price_for("gpt-4o-mini"))
        with self.assertRaises(UnknownModel):
            price_for("gpt-unknown")

    def test_reserve_is_refused_before_the_total_would_pass_the_limit(self):
        budget = Budget(0.002, self.ledger)
        first = budget.reserve(kind="generation", model="gpt-4o-mini", input_bytes=3000, max_output_tokens=1000)
        budget.settle(first, 0.0004, "actual")
        self.assertAlmostEqual(budget.spent(), 0.0004)
        second = budget.reserve(kind="generation", model="gpt-4o-mini", input_bytes=3000, max_output_tokens=1000)
        budget.settle(second, 0.0004, "actual")
        big_worst = cost_usd("gpt-4o-mini", 4_000, 4_000)          # 0.003 > what is left
        self.assertGreater(big_worst, 0.002 - budget.spent())
        with self.assertRaises(BudgetStop):
            budget.reserve(kind="generation", model="gpt-4o-mini", input_bytes=6000, max_output_tokens=4_000)

    def test_the_stop_is_permanent_even_for_a_tiny_call(self):
        budget = Budget(0.0001, self.ledger)
        with self.assertRaises(BudgetStop):
            budget.reserve(kind="generation", model="gpt-4o-mini", input_bytes=60_000, max_output_tokens=2000)
        with self.assertRaises(BudgetStop):
            budget.reserve(kind="summary", model="gpt-4o-mini", input_bytes=10, max_output_tokens=1)
        self.assertEqual(budget.spent(), 0)
        self.assertFalse(self.ledger.exists() and self.ledger.read_text(encoding="utf-8").strip())

    def test_an_unsettled_reserve_keeps_counting_at_its_worst_case(self):
        budget = Budget(1.0, self.ledger)
        reservation = budget.reserve(kind="generation", model="gpt-4o-mini", input_bytes=3000, max_output_tokens=1000)
        self.assertAlmostEqual(Budget(1.0, self.ledger).spent(), reservation.worst_usd)    # a new process sees it too

    def test_the_ledger_holds_only_labels_and_numbers(self):
        budget = Budget(1.0, self.ledger)
        reservation = budget.reserve(kind="judge", model="gpt-6.1-sol", input_bytes=100, max_output_tokens=100, config="judge", q="N01")
        budget.settle(reservation, 0.01, "actual", outcome="ok")
        for line in self.ledger.read_text(encoding="utf-8").splitlines():
            self.assertLess(len(line), 300)
            self.assertEqual(set(json.loads(line)) - {"ev", "id", "kind", "config", "q", "model", "worst_usd", "cost_usd",
                                                      "basis", "outcome", "provider"}, set())


class MeteredClientTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def client(self, behaviour, limit=1.0):
        inner = FakeInner(behaviour)
        budget = Budget(limit, self.dir / "spend.jsonl")
        return inner, budget, MeteredClient(inner, budget, JsonlWriter(self.dir / "calls.jsonl"), config="C0")

    def create(self, metered, size=1000, **extra):
        return metered.chat.completions.create(model="gpt-4o-mini", messages=messages(size), max_tokens=1000,
                                               response_format={"type": "json_schema"}, **extra)

    def test_a_good_call_is_charged_by_the_provider_numbers(self):
        inner, budget, metered = self.client(lambda: response(2000, 500))
        self.create(metered)
        self.assertAlmostEqual(budget.spent(), cost_usd("gpt-4o-mini", 2000, 500))
        self.assertEqual(metered.records[0]["basis"], "actual")

    def test_a_server_error_is_charged_at_the_worst_case_and_is_not_repeated(self):
        inner, budget, metered = self.client(lambda: StatusError(500))
        with self.assertRaises(StatusError):
            self.create(metered)
        self.assertEqual(inner.calls, 1)
        self.assertGreater(budget.spent(), 0)
        self.assertEqual(metered.records[0]["basis"], "worst_case")
        self.assertEqual(metered.records[0]["reason"], "server_error")

    def test_a_timeout_is_ambiguous_so_charged_at_the_worst_case(self):
        inner, budget, metered = self.client(lambda: APITimeoutError())
        with self.assertRaises(APITimeoutError):
            self.create(metered)
        self.assertEqual((inner.calls, metered.records[0]["reason"], metered.records[0]["basis"]), (1, "timeout", "worst_case"))

    def test_a_refused_request_costs_nothing_and_keeps_no_provider_text(self):
        inner, budget, metered = self.client(lambda: StatusError(400))
        with self.assertRaises(StatusError):
            self.create(metered)
        self.assertEqual(budget.spent(), 0)
        self.assertNotIn("secret provider text", (self.dir / "calls.jsonl").read_text(encoding="utf-8"))

    def test_no_provider_call_is_made_once_the_budget_would_be_passed(self):
        inner, budget, metered = self.client(lambda: response(), limit=0.0001)
        with self.assertRaises(BudgetStop):
            self.create(metered, size=20_000)
        self.assertEqual(inner.calls, 0)
        self.assertTrue(metered.budget_stopped)
        self.assertEqual(metered.records[0]["outcome"], "budget_stop")

    def test_the_stream_path_is_refused_before_the_stream_exists(self):
        inner, budget, metered = self.client(lambda: response(), limit=0.0001)
        with self.assertRaises(BudgetStop):
            metered.beta.chat.completions.stream(model="gpt-4o-mini", messages=messages(20_000), max_tokens=1000)
        self.assertEqual(inner.calls, 0)

    def test_a_stream_that_never_finishes_is_charged_at_the_worst_case(self):
        inner, budget, metered = self.client(lambda: StatusError(502))
        with self.assertRaises(StatusError):
            with metered.beta.chat.completions.stream(model="gpt-4o-mini", messages=messages(1000), max_tokens=1000) as stream:
                for _ in stream:
                    pass
                stream.get_final_completion()
        self.assertEqual(metered.records[0]["basis"], "worst_case")

    def test_a_finished_stream_records_time_to_first_fragment_and_actual_usage(self):
        inner, budget, metered = self.client(lambda: response(500, 100))
        with metered.beta.chat.completions.stream(model="gpt-4o-mini", messages=messages(1000), max_tokens=1000) as stream:
            for _ in stream:
                pass
            stream.get_final_completion()
        record = metered.records[0]
        self.assertIsNotNone(record["ttfb_ms"])
        self.assertAlmostEqual(record["cost_usd"], cost_usd("gpt-4o-mini", 500, 100))

    def test_the_judge_goes_through_the_same_budget_and_ledger(self):
        inner, budget, metered = self.client(lambda: response(1000, 200, model="gpt-6.1-sol"), limit=0.0001)
        with self.assertRaises(BudgetStop):
            judge_call(metered, "N01", model="gpt-6.1-sol", messages=messages(20_000), max_completion_tokens=3000)
        self.assertEqual(inner.calls, 0)


def run_cli(*args, env_extra=None, key_file=None):
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith(("OPENAI_", "ANTHROPIC_"))}
    env.update(PYTHONUTF8="1", **(env_extra or {}))
    return subprocess.run([sys.executable, "-m", "tools.chat_eval", *args], cwd=str(REPO), env=env, capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=600)


class EndToEndBudgetTests(unittest.TestCase):
    """The worker runs the product path against a local fake provider; nothing leaves this computer."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name) / "run"

    def run_c0(self, server, only, budget, extra=()):
        return run_cli("run", "--configs", "C0", "--runs", "1", "--only", only, "--base-repo", str(REPO),
                       "--candidate-repo", str(REPO), "--run-dir", str(self.dir), "--base-url", server.url,
                       "--budget-usd", str(budget), *extra)

    def answers(self):
        return [json.loads(line) for line in (self.dir / "answers.jsonl").read_text(encoding="utf-8").splitlines()]

    def test_the_run_stops_before_the_call_that_would_pass_the_limit(self):
        with FakeOpenAI(usage=(10000, 1000)) as server:        # about $0.0021 per answer at gpt-4o-mini prices
            result = self.run_c0(server, "N01,N02,N03,N04,N05,N06,N07", 0.009)
            made = server.count("generation")
        self.assertEqual(result.returncode, 3, result.stdout[-800:])
        answers = self.answers()
        self.assertEqual(answers[-1]["outcome"], "budget_stop")
        done = [a for a in answers if a["outcome"] == "ok"]
        self.assertEqual(made, len(done))                      # the refused call never reached the provider
        self.assertTrue(3 <= made <= 5, made)
        self.assertLessEqual(Budget(9, self.dir / "spend.jsonl").spent(), 0.009)
        self.assertIn("chat_eval.worker outcome=stopped", result.stdout)

    def test_a_provider_failure_is_recorded_and_never_repeated(self):
        with FakeOpenAI(mode="http500") as server:
            result = self.run_c0(server, "N01,N02", 8.5)
            made = server.count()
        self.assertEqual(result.returncode, 0, result.stdout[-800:])
        self.assertEqual(made, 2)                              # one request per question: no SDK retries, no re-asking
        answers = self.answers()
        self.assertEqual([a["outcome"] for a in answers], ["server_error", "server_error"])
        self.assertGreater(Budget(8.5, self.dir / "spend.jsonl").spent(), 0)      # an ambiguous failure still counts

    def test_a_rejected_request_costs_nothing_and_its_message_is_not_kept(self):
        with FakeOpenAI(mode="bad_request") as server:
            result = self.run_c0(server, "N01", 8.5)
        self.assertEqual(result.returncode, 0, result.stdout[-800:])
        self.assertEqual(self.answers()[0]["outcome"], "rejected")
        self.assertEqual(Budget(8.5, self.dir / "spend.jsonl").spent(), 0)
        calls = (self.dir / "calls.jsonl").read_text(encoding="utf-8")
        self.assertIn('"provider_param": "max_tokens"', calls)
        for path in self.dir.iterdir():
            self.assertNotIn("Unsupported parameter", path.read_text(encoding="utf-8"), path.name)

    def test_a_limit_above_the_owners_ten_dollars_is_refused(self):
        with FakeOpenAI() as server:
            result = self.run_c0(server, "N01", 11)
        self.assertEqual(result.returncode, 2)
        self.assertIn("reason=budget_out_of_range", result.stdout)
        self.assertFalse((self.dir / "answers.jsonl").exists())


if __name__ == "__main__":
    unittest.main()
