"""CHAT-CLAUDE step 3, E-1..E-4: Anthropic prices and cache arithmetic, separate per-provider caps in the shared ledger,
the key and the addresses, and the call counter MeteredAnthropic against the REAL anthropic SDK talking to a loopback fake.

No real API call anywhere: the SDK is pointed at tests/chat_eval_fake_server.FakeAnthropic on 127.0.0.1.
Modules are imported whole and attributes are read inside the tests, so a missing behaviour is a failing test, not an
import error of the file.
"""
import json
import os
import tempfile
import unittest

import chat_eval_secrets_guard  # noqa: F401  (no test of this run may open .secrets; tests/chat_eval_secrets_guard.py)
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import anthropic

from chat_eval_fake_server import FakeAnthropic
from tools.chat_eval import budget, cli, common, metered

HAIKU, SONNET, OPUS = "claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5"


def lines(path):
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]


class AnthropicPriceTests(unittest.TestCase):
    """E-1."""

    def test_prices_follow_the_design_table(self):
        self.assertEqual(budget.price_for(HAIKU), (0.10, 0.50))
        self.assertEqual(budget.price_for(SONNET), (2.0, 10.0))
        self.assertEqual(budget.price_for(OPUS), (4.0, 20.0))
        self.assertEqual(budget.cache_prices(HAIKU), (0.125, 0.01))
        self.assertEqual(budget.cache_prices(SONNET), (2.5, 0.10))
        self.assertEqual(budget.cache_prices(OPUS), (5.0, 0.20))
        with self.assertRaises(budget.UnknownModel):
            budget.price_for("claude-unknown-9")
        with self.assertRaises(budget.UnknownModel):
            budget.cache_prices("claude-unknown-9")

    def test_a_call_costs_input_plus_cache_write_plus_cache_read_plus_output(self):
        million = dict(input_tokens=1_000_000, cache_write_tokens=1_000_000, cache_read_tokens=1_000_000, output_tokens=1_000_000)
        self.assertAlmostEqual(budget.anthropic_cost_usd(HAIKU, **million), 0.10 + 0.125 + 0.01 + 0.50, places=9)
        self.assertAlmostEqual(budget.anthropic_cost_usd(SONNET, **million), 2 + 2.5 + 0.10 + 10, places=9)
        self.assertAlmostEqual(budget.anthropic_cost_usd(OPUS, **million), 4 + 5 + 0.20 + 20, places=9)
        self.assertAlmostEqual(budget.anthropic_cost_usd(HAIKU, input_tokens=120, cache_write_tokens=800,
                                                         cache_read_tokens=200, output_tokens=300), 2.64e-4, places=9)
        self.assertEqual(budget.anthropic_cost_usd(HAIKU, input_tokens=0, cache_write_tokens=0, cache_read_tokens=0,
                                                   output_tokens=0), 0)

    def test_the_worst_case_is_the_dearest_input_price_plus_the_whole_output_ceiling(self):
        # 60000 bytes -> 40000 tokens at the safe upper rate of 1.5 bytes per token; written to cache at 0.125 per million
        worst = budget.worst_case_usd(HAIKU, input_bytes=60000, max_output_tokens=4000)
        self.assertAlmostEqual(worst, 40000 * 0.125 / 1e6 + 4000 * 0.50 / 1e6, places=9)
        self.assertGreater(worst, budget.anthropic_cost_usd(HAIKU, input_tokens=40000, cache_write_tokens=0,
                                                            cache_read_tokens=0, output_tokens=4000))
        self.assertAlmostEqual(budget.worst_case_usd(OPUS, input_bytes=3000, max_output_tokens=5000),
                               2000 * 5.0 / 1e6 + 5000 * 20.0 / 1e6, places=9)

    def test_the_openai_worst_case_is_unchanged(self):
        self.assertAlmostEqual(budget.worst_case_usd("gpt-4o-mini", input_bytes=3000, max_output_tokens=1000),
                               budget.cost_usd("gpt-4o-mini", 2000, 1000), places=12)

    def test_haiku_with_an_upper_input_estimate_over_100k_tokens_is_refused_before_a_call(self):
        budget.worst_case_usd(HAIKU, input_bytes=150_000, max_output_tokens=100)           # exactly 100000 tokens: allowed
        with self.assertRaises(budget.LongContextPrice):
            budget.worst_case_usd(HAIKU, input_bytes=150_001, max_output_tokens=100)
        budget.worst_case_usd(SONNET, input_bytes=300_000, max_output_tokens=100)          # one price grid for Sonnet and Opus
        budget.worst_case_usd(OPUS, input_bytes=300_000, max_output_tokens=100)

    def test_the_long_context_refusal_reaches_the_ledger_as_no_call_and_no_line(self):
        with tempfile.TemporaryDirectory() as tmp, FakeAnthropic() as server:
            ledger = Path(tmp) / "spend.jsonl"
            client = anthropic.Anthropic(api_key="sk-ant-test-" + "k" * 20, base_url=server.url, max_retries=0)
            counted = metered.MeteredAnthropic(client, budget.Budget(5.0, ledger, provider="anthropic"),
                                               metered.JsonlWriter(Path(tmp) / "calls.jsonl"), config="H3")
            with self.assertRaises(budget.LongContextPrice):
                counted.messages.create(model=HAIKU, max_tokens=100,
                                        messages=[{"role": "user", "content": "я" * 80_000}])      # 160000 bytes
            self.assertEqual(server.count(), 0)
            self.assertFalse(ledger.exists() and ledger.read_text(encoding="utf-8").strip())


class PerProviderCapTests(unittest.TestCase):
    """E-2."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ledger = Path(self.tmp.name) / "spend.jsonl"

    def reserve(self, provider, model, usd=None, **extra):
        b = budget.Budget(extra.pop("limit", 8.5), self.ledger, provider=provider)
        r = b.reserve(kind="generation", model=model, input_bytes=3000, max_output_tokens=1000)
        if usd is not None:
            b.settle(r, usd, "actual")
        return b, r

    def test_every_new_ledger_line_names_its_provider_and_old_lines_are_openai(self):
        self.reserve("openai", "gpt-4o-mini", 0.01)
        self.reserve("anthropic", HAIKU, 0.02, limit=5.0)
        rows = lines(self.ledger)
        self.assertEqual({(r["ev"], r["provider"]) for r in rows},
                         {("reserve", "openai"), ("settle", "openai"), ("reserve", "anthropic"), ("settle", "anthropic")})
        legacy = Path(self.tmp.name) / "legacy.jsonl"
        legacy.write_text(
            json.dumps({"ev": "reserve", "id": "aaaa", "kind": "generation", "model": "gpt-4o-mini", "worst_usd": 6.0}) + "\n" +
            json.dumps({"ev": "settle", "id": "aaaa", "cost_usd": 5.0, "basis": "actual"}) + "\n", encoding="utf-8")
        self.assertAlmostEqual(budget.Budget(8.5, legacy).spent(), 5.0)                           # an old line counts as OpenAI
        self.assertEqual(budget.Budget(5.0, legacy, provider="anthropic").spent(), 0)
        self.assertEqual(budget.spent_by_provider(legacy), {"openai": 5.0, "anthropic": 0.0})

    def test_the_caps_are_separate_but_the_ledger_is_one(self):
        self.reserve("openai", "gpt-4o-mini", 8.49)
        self.reserve("anthropic", HAIKU, 3.0, limit=5.0)                                          # not blocked by the OpenAI spend
        by = budget.spent_by_provider(self.ledger)
        self.assertAlmostEqual(by["openai"], 8.49)
        self.assertAlmostEqual(by["anthropic"], 3.0)
        with self.assertRaises(budget.BudgetStop):                                                # the OpenAI total is 8.5, 8.49 is spent
            budget.Budget(8.5, self.ledger).reserve(kind="generation", model="gpt-4o-mini", input_bytes=300_000,
                                                    max_output_tokens=4000)
        self.assertAlmostEqual(budget.Budget(5.0, self.ledger, provider="anthropic").spent(), 3.0)

    def test_the_openai_cap_values_are_the_old_ones(self):
        # DECISIONS 17: the owner OpenAI project limit is 9 USD, the hard cap of the whole eval is 8.5
        self.assertEqual(common.BUDGET_DEFAULT_USD, 8.5)
        self.assertEqual(common.OWNER_LIMIT_USD, 9.0)
        budget.Budget(8.5, self.ledger)
        budget.Budget(9.0, self.ledger)
        for bad in (0.0, -1.0, 9.01, 10.0, float("nan"), float("inf")):
            with self.assertRaises(ValueError, msg=str(bad)):
                budget.Budget(bad, self.ledger)

    def test_the_anthropic_cap_is_9_5_inside_the_10_dollars_of_credits_and_zero_means_unset(self):
        self.assertEqual(common.ANTHROPIC_EVAL_LIMIT_USD, 9.5)
        self.assertEqual(common.ANTHROPIC_OWNER_LIMIT_USD, 10.0)
        self.assertTrue(common.valid_anthropic_budget(0))
        self.assertTrue(common.valid_anthropic_budget(10))
        for bad in (-0.01, 10.01, float("nan"), float("inf"), True, None, "5"):
            self.assertFalse(common.valid_anthropic_budget(bad), bad)
        for bad in (-0.01, 10.01, float("nan"), float("inf")):
            with self.assertRaises(ValueError, msg=str(bad)):
                budget.Budget(bad, self.ledger, provider="anthropic")

    def test_with_the_cap_at_zero_a_paid_anthropic_call_refuses_without_the_network_and_without_a_ledger_line(self):
        b = budget.Budget(0.0, self.ledger, provider="anthropic")
        with self.assertRaises(budget.BudgetUnset):
            b.reserve(kind="generation", model=HAIKU, input_bytes=10, max_output_tokens=1)
        self.assertTrue(issubclass(budget.BudgetUnset, budget.BudgetStop))
        self.assertFalse(self.ledger.exists() and self.ledger.read_text(encoding="utf-8").strip())
        with FakeAnthropic() as server:
            client = anthropic.Anthropic(api_key="sk-ant-test-" + "k" * 20, base_url=server.url, max_retries=0)
            counted = metered.MeteredAnthropic(client, b, metered.JsonlWriter(Path(self.tmp.name) / "calls.jsonl"), config="H3")
            with self.assertRaises(budget.BudgetUnset):
                counted.messages.create(model=HAIKU, max_tokens=50, messages=[{"role": "user", "content": "x"}])
            self.assertEqual(server.count(), 0)
        record = lines(Path(self.tmp.name) / "calls.jsonl")[0]
        self.assertEqual((record["outcome"], record["reason"], record["provider"]), ("budget_stop", "budget_unset", "anthropic"))

    def test_a_reservation_is_the_worst_case_cache_write_plus_max_tokens(self):
        b, r = self.reserve("anthropic", HAIKU, limit=5.0)
        self.assertAlmostEqual(r.worst_usd, budget.worst_case_usd(HAIKU, input_bytes=3000, max_output_tokens=1000), places=12)
        self.assertEqual(lines(self.ledger)[0]["worst_usd"], r.worst_usd)
        b.settle(r, 0.001, "actual")
        again = budget.Budget(5.0, self.ledger, provider="anthropic").reserve(kind="judge", model=OPUS, input_bytes=3000,
                                                                              max_output_tokens=5000)
        self.assertGreater(again.worst_usd, 0)

    def test_the_cap_refuses_before_the_call_that_would_pass_it(self):
        b = budget.Budget(0.001, self.ledger, provider="anthropic")
        with self.assertRaises(budget.BudgetStop):
            b.reserve(kind="generation", model=HAIKU, input_bytes=60_000, max_output_tokens=4000)      # worst 0.007
        self.assertEqual(b.spent(), 0)

    def test_a_model_of_the_other_provider_is_refused(self):
        with self.assertRaises(ValueError):
            budget.Budget(5.0, self.ledger, provider="anthropic").reserve(kind="generation", model="gpt-4o-mini",
                                                                          input_bytes=10, max_output_tokens=1)
        with self.assertRaises(ValueError):
            budget.Budget(5.0, self.ledger, provider="openai").reserve(kind="generation", model=HAIKU,
                                                                       input_bytes=10, max_output_tokens=1)
        self.assertEqual(common.provider_of(HAIKU), "anthropic")
        self.assertEqual(common.provider_of("gpt-6-luna"), "openai")


class KeyAndAddressTests(unittest.TestCase):
    """E-3."""

    def test_the_key_comes_from_the_owners_file_into_the_process_environment_only(self):
        key = "sk-ant-test-" + uuid4().hex
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ANTHROPIC_API_KEY", None)
            path = Path(tmp) / "anthropic-eval.env"
            path.write_text("OTHER=1\nANTHROPIC_API_KEY=%s\n" % key, encoding="utf-8")
            self.addCleanup(common._SECRETS.discard, key)
            common.load_key(path, "ANTHROPIC_API_KEY")
            self.assertTrue(os.environ["ANTHROPIC_API_KEY"] == key)   # not assertEqual: it prints both values
            self.assertEqual(common.redact("boom " + key), "boom [key]")
            path.write_text("ANTHROPIC_API_KEY=\n", encoding="utf-8")
            with self.assertRaises(common.KeyError_) as caught:
                common.load_key(path, "ANTHROPIC_API_KEY")
            self.assertEqual(str(caught.exception), "key_missing_in_file")
            with self.assertRaises(common.KeyError_) as caught:
                common.load_key(Path(tmp) / "none.env", "ANTHROPIC_API_KEY")
            self.assertEqual(str(caught.exception), "key_file_missing")

    def test_the_default_key_files_are_the_owners_files_outside_git(self):
        self.assertEqual(common.default_key_file("anthropic").name, "anthropic-eval.env")
        self.assertEqual(common.default_key_file("anthropic").parent.name, ".secrets")
        self.assertEqual(common.default_key_file().name, "openai-eval.env")

    def test_a_provider_that_repeats_the_key_in_its_error_is_still_masked(self):
        key = "sk-ant-test-" + uuid4().hex
        self.addCleanup(common._SECRETS.discard, key)
        common.register_secret(key)
        with tempfile.TemporaryDirectory() as tmp:
            writer = metered.JsonlWriter(Path(tmp) / "calls.jsonl")
            writer.write({"detail": "invalid x-api-key: " + key})                # the mutation: a field that carries the key
            self.assertNotIn(key, (Path(tmp) / "calls.jsonl").read_text(encoding="utf-8"))
            self.assertIn("[key]", (Path(tmp) / "calls.jsonl").read_text(encoding="utf-8"))
            with FakeAnthropic(expected_key=key, mode="unauthorized_echo") as server:
                client = anthropic.Anthropic(api_key=key, base_url=server.url, max_retries=0)
                counted = metered.MeteredAnthropic(client, budget.Budget(5.0, Path(tmp) / "spend.jsonl", provider="anthropic"),
                                                   metered.JsonlWriter(Path(tmp) / "c2.jsonl"), config="H3")
                with self.assertRaises(anthropic.AuthenticationError) as caught:
                    counted.messages.create(model=HAIKU, max_tokens=50, messages=[{"role": "user", "content": "x"}])
                self.assertIn(key, str(caught.exception))                        # the provider really echoed it
            for path in Path(tmp).iterdir():
                self.assertNotIn(key, path.read_text(encoding="utf-8"), path.name)
            record = lines(Path(tmp) / "c2.jsonl")[0]
            self.assertEqual((record["reason"], record["provider_status"], record["provider_code"]),
                             ("rejected", 401, "authentication_error"))

    def test_the_child_environment_has_no_inherited_anthropic_or_openai_values_and_only_its_own_key(self):
        hostile = {"ANTHROPIC_BASE_URL": "https://proxy.example.invalid", "ANTHROPIC_MODEL": "x", "ANTHROPIC_CHAT_MODEL": "y",
                   "ANTHROPIC_LOG": "debug", "ANTHROPIC_AUTH_TOKEN": "tok", "ANTHROPIC_API_KEY": "sk-ant-real-looking-" + "z" * 20,
                   "OPENAI_API_KEY": "sk-openai-real-looking-" + "z" * 20, "OPENAI_BASE_URL": "https://proxy.example.invalid/v1",
                   "LUNARIA_CHAT_PROVIDER": "openai"}
        with patch.dict(os.environ, hostile):
            dry = common.child_env(real=False, provider="anthropic", base_url="http://127.0.0.1:8102")
            self.assertEqual(dry["ANTHROPIC_BASE_URL"], "http://127.0.0.1:8102")
            self.assertEqual(dry["ANTHROPIC_API_KEY"], common.STUB_ANTHROPIC_KEY)
            self.assertTrue(common.STUB_ANTHROPIC_KEY.startswith("sk-ant-"))
            for name in ("ANTHROPIC_MODEL", "ANTHROPIC_CHAT_MODEL", "ANTHROPIC_LOG", "ANTHROPIC_AUTH_TOKEN",
                         "OPENAI_API_KEY", "OPENAI_BASE_URL", "LUNARIA_CHAT_PROVIDER"):
                self.assertNotIn(name, dry, name)
            real = common.child_env(real=True, provider="anthropic")
            self.assertEqual(real["ANTHROPIC_API_KEY"], hostile["ANTHROPIC_API_KEY"])
            self.assertEqual(real["ANTHROPIC_BASE_URL"], "https://api.anthropic.com")
            self.assertNotIn("OPENAI_API_KEY", real)
            for name in ("ANTHROPIC_MODEL", "ANTHROPIC_CHAT_MODEL", "ANTHROPIC_LOG", "ANTHROPIC_AUTH_TOKEN"):
                self.assertNotIn(name, real, name)
            openai_real = common.child_env(real=True)
            self.assertEqual(openai_real["OPENAI_API_KEY"], hostile["OPENAI_API_KEY"])
            self.assertFalse([n for n in openai_real if n.startswith("ANTHROPIC_")])        # no Anthropic value reaches an OpenAI run
            openai_dry = common.child_env(real=False)
            self.assertFalse([n for n in openai_dry if n.startswith("ANTHROPIC_")])

    def test_the_provider_address_rule_for_anthropic(self):
        self.assertEqual(common.ANTHROPIC_URL, "https://api.anthropic.com")
        self.assertEqual(common.STAND_ANTHROPIC_URL, "http://127.0.0.1:8102")
        self.assertEqual(common.client_base_url(real=True, dry_url="http://127.0.0.1:1", provider="anthropic"),
                         "https://api.anthropic.com")
        self.assertEqual(common.client_base_url(real=True, dry_url="http://127.0.0.1:1", real_base_url="http://127.0.0.1:5",
                                                provider="anthropic"), "http://127.0.0.1:5")
        self.assertEqual(common.client_base_url(real=False, dry_url="http://127.0.0.1:1", provider="anthropic"), "http://127.0.0.1:1")
        self.assertEqual(common.client_base_url(real=True, dry_url="http://127.0.0.1:1"), "https://api.openai.com/v1")

    def paid_args(self, tmp, **extra):
        return Namespace(real=True, run_dir=str(Path(tmp) / "run"), ledger_dir="", real_base_url="", budget_usd=8.5,
                         key_file="", base_url=common.STAND_OPENAI_URL, command="judge", judges="claude", configs="",
                         anthropic_key_file=str(self.write_key(tmp)), anthropic_base_url=common.STAND_ANTHROPIC_URL,
                         anthropic_real_base_url="", anthropic_budget_usd=5.0, **extra)

    def write_key(self, tmp):
        path = Path(tmp) / "anthropic-eval.env"
        path.write_text("ANTHROPIC_API_KEY=sk-ant-test-%s\n" % uuid4().hex, encoding="utf-8")
        return path

    def test_the_paid_client_points_at_the_official_address_whatever_the_environment_says(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"ANTHROPIC_BASE_URL": "https://proxy.example.invalid",
                                                                           "ANTHROPIC_API_KEY": "sk-ant-test-not-used-anywhere"}):
            counted = cli.make_metered_claude(self.paid_args(tmp), Path(tmp) / "run", Path(tmp) / "spend.jsonl", "judge")
            self.assertEqual(str(counted.inner.base_url).rstrip("/"), "https://api.anthropic.com")
            self.assertEqual(counted.inner.max_retries, 0)

    def test_prepare_drops_an_inherited_base_url_and_loads_only_the_anthropic_key_for_an_anthropic_command(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"ANTHROPIC_BASE_URL": "https://proxy.example.invalid",
                                                                           "OPENAI_API_KEY": "sk-openai-must-stay-out-" + "q" * 10}):
            os.environ.pop("ANTHROPIC_API_KEY", None)
            self.addCleanup(common._SECRETS.clear)
            args = self.paid_args(tmp)
            cli.prepare(args)
            self.assertFalse("ANTHROPIC_BASE_URL" in os.environ)   # never assertNotIn(os.environ): a failure would print the environment
            self.assertTrue(os.environ["ANTHROPIC_API_KEY"].startswith("sk-ant-test-"))

    def test_the_dry_mode_replaces_a_real_key_with_the_stub_and_refuses_an_outside_address(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-real-looking-" + "z" * 20}):
            args = self.paid_args(tmp)
            args.real = False
            cli.prepare(args)
            self.assertTrue(os.environ["ANTHROPIC_API_KEY"] == common.STUB_ANTHROPIC_KEY)   # not assertEqual: it prints both values
            args.anthropic_base_url = "https://api.anthropic.com"
            with self.assertRaises(SystemExit) as caught:
                cli.prepare(args)
            self.assertEqual(caught.exception.code, 2)

    def test_prepare_refuses_a_paid_anthropic_command_with_the_cap_unset_before_the_key_is_read(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {}):
            os.environ.pop("ANTHROPIC_API_KEY", None)
            args = self.paid_args(tmp)
            args.anthropic_budget_usd = 0.0
            args.anthropic_key_file = str(Path(tmp) / "does-not-exist.env")
            with self.assertRaises(SystemExit) as caught:
                cli.prepare(args)
            self.assertEqual(caught.exception.code, 2)
            self.assertFalse("ANTHROPIC_API_KEY" in os.environ)


def make_counted(tmp, server, *, limit=5.0, config="H3", timeout=20, ledger=None):
    client = anthropic.Anthropic(api_key="sk-ant-test-" + "k" * 20, base_url=server.url, max_retries=0, timeout=timeout)
    b = budget.Budget(limit, ledger or Path(tmp) / "spend.jsonl", provider="anthropic")
    return metered.MeteredAnthropic(client, b, metered.JsonlWriter(Path(tmp) / "calls.jsonl"), config=config), b


MESSAGES = [{"role": "user", "content": "Какие у меня сильные стороны?"}]
FORMAT = {"effort": "low", "format": {"type": "json_schema", "schema": {"type": "object", "additionalProperties": False,
                                                                       "required": ["answer"], "properties": {"answer": {"type": "string"}}}}}


class MeteredAnthropicTests(unittest.TestCase):
    """E-4: the two entry points the product uses, messages.stream and messages.create."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def stream(self, counted, **extra):
        kwargs = dict(model=HAIKU, max_tokens=4000, messages=MESSAGES, system="Ты помощник.", thinking={"type": "adaptive"},
                      output_config=FORMAT)
        kwargs.update(extra)
        with counted.messages.stream(**kwargs) as stream:
            events = [event.type for event in stream]
            final = stream.get_final_message()
        return events, final

    def records(self):
        return lines(Path(self.tmp.name) / "calls.jsonl")

    def test_a_good_stream_is_charged_by_the_four_usage_fields_and_records_what_the_provider_returned(self):
        with FakeAnthropic(usage=(120, 800, 200, 300)) as server:
            counted, b = make_counted(self.tmp.name, server)
            events, final = self.stream(counted)
            self.assertEqual(server.count(), 1)
        self.assertEqual(final.stop_reason, "end_turn")
        self.assertIn("text", events)
        expected = budget.anthropic_cost_usd(HAIKU, input_tokens=120, cache_write_tokens=800, cache_read_tokens=200, output_tokens=300)
        self.assertAlmostEqual(b.spent(), expected, places=9)
        record = self.records()[0]
        self.assertEqual((record["provider"], record["kind"], record["outcome"], record["basis"], record["config"]),
                         ("anthropic", "generation", "ok", "actual", "H3"))
        self.assertEqual((record["input_tokens"], record["cache_creation_input_tokens"], record["cache_read_input_tokens"],
                          record["output_tokens"]), (120, 800, 200, 300))
        self.assertEqual((record["stop_reason"], record["refusal"], record["model_returned"], record["model_requested"]),
                         ("end_turn", "none", HAIKU, HAIKU))
        self.assertAlmostEqual(record["cost_usd"], expected, places=9)
        self.assertEqual(record["reserved_usd"] > record["cost_usd"], True)

    def test_time_to_first_text_is_the_first_text_delta_on_the_monotonic_clock(self):
        with FakeAnthropic(delay=0.4) as server:
            counted, _ = make_counted(self.tmp.name, server)
            self.stream(counted)
        record = self.records()[0]
        self.assertGreaterEqual(record["ttfb_ms"], 350)
        self.assertLessEqual(record["ttfb_ms"], record["duration_ms"])

    def test_text_stream_helper_also_sets_time_to_first_text(self):
        with FakeAnthropic(delay=0.2) as server:
            counted, _ = make_counted(self.tmp.name, server)
            with counted.messages.stream(model=HAIKU, max_tokens=4000, messages=MESSAGES, output_config=FORMAT) as stream:
                text = "".join(stream.text_stream)
                stream.get_final_message()
        self.assertIn("answer", text)
        self.assertGreaterEqual(self.records()[0]["ttfb_ms"], 150)

    def test_the_reservation_is_made_before_the_call_and_the_refusal_is_free_of_network(self):
        with FakeAnthropic() as server:
            counted, b = make_counted(self.tmp.name, server, limit=0.0001)
            with self.assertRaises(budget.BudgetStop):
                counted.messages.stream(model=HAIKU, max_tokens=4000, messages=[{"role": "user", "content": "я" * 30_000}])
            with self.assertRaises(budget.BudgetStop):
                counted.messages.create(model=HAIKU, max_tokens=4000, messages=[{"role": "user", "content": "я" * 30_000}])
            self.assertEqual(server.count(), 0)
        self.assertTrue(counted.budget_stopped)
        self.assertEqual({r["outcome"] for r in self.records()}, {"budget_stop"})

    def test_create_without_a_format_is_a_summary_and_with_a_format_a_generation(self):
        with FakeAnthropic() as server:
            counted, _ = make_counted(self.tmp.name, server)
            counted.messages.create(model=HAIKU, max_tokens=2900, messages=MESSAGES, thinking={"type": "adaptive"},
                                    output_config={"effort": "low"})
            counted.messages.create(model=HAIKU, max_tokens=4000, messages=MESSAGES, output_config=FORMAT)
        self.assertEqual([r["kind"] for r in self.records()], ["summary", "generation"])
        self.assertEqual([r["outcome"] for r in self.records()], ["ok", "ok"])

    def test_max_tokens_is_charged_for_what_was_generated_and_recorded_as_the_stop_reason(self):
        with FakeAnthropic(mode="max_tokens", usage=(100, 0, 500, 4000)) as server:
            counted, b = make_counted(self.tmp.name, server)
            _, final = self.stream(counted)
        self.assertEqual(final.stop_reason, "max_tokens")
        self.assertEqual(self.records()[0]["stop_reason"], "max_tokens")
        self.assertAlmostEqual(b.spent(), budget.anthropic_cost_usd(HAIKU, input_tokens=100, cache_write_tokens=0,
                                                                    cache_read_tokens=500, output_tokens=4000), places=9)

    def test_a_refusal_records_only_the_closed_category_never_the_explanation(self):
        for mode in ("refusal", "refusal_mid"):
            with self.subTest(mode=mode), FakeAnthropic(mode=mode) as server:
                counted, _ = make_counted(self.tmp.name, server)
                _, final = self.stream(counted)
                self.assertEqual(final.stop_reason, "refusal")
        for record in self.records():
            self.assertEqual((record["stop_reason"], record["refusal"], record["outcome"]), ("refusal", "general_harms", "ok"))
        for path in Path(self.tmp.name).iterdir():
            self.assertNotIn("CANARY", path.read_text(encoding="utf-8"), path.name)
        self.assertEqual([metered.refusal_label(x) for x in ("cyber", "bio", "frontier_llm", "general_harms", "reasoning_extraction",
                                                             None, "something_new", "bad label!")],
                         ["cyber", "bio", "frontier_llm", "general_harms", "reasoning_extraction", "none", "other", "other"])

    def test_a_substituted_model_stops_the_configuration_after_the_call_is_paid_by_its_usage(self):
        with FakeAnthropic(mode="wrong_model", usage=(100, 0, 0, 100)) as server:
            counted, b = make_counted(self.tmp.name, server)
            with self.assertRaises(metered.ModelSubstituted):
                self.stream(counted)
            self.assertEqual(server.count(), 1)
            self.assertEqual(counted.stop_reason, "model_substituted")
            self.assertAlmostEqual(b.spent(), budget.anthropic_cost_usd(SONNET, input_tokens=100, cache_write_tokens=0,
                                                                        cache_read_tokens=0, output_tokens=100), places=9)
            with self.assertRaises(metered.ModelSubstituted):                                # no further call is made
                counted.messages.create(model=HAIKU, max_tokens=50, messages=MESSAGES)
            self.assertEqual(server.count(), 1)
        record = self.records()[0]
        self.assertEqual((record["outcome"], record["reason"], record["model_returned"]), ("error", "model_substituted", SONNET))

    def test_a_rejected_request_costs_nothing_and_keeps_only_status_and_code(self):
        for mode, status, code in (("bad_request", 400, "invalid_request_error"), ("rate_limit", 429, "rate_limit_error")):
            with self.subTest(mode=mode), FakeAnthropic(mode=mode) as server:
                counted, b = make_counted(self.tmp.name, server)
                with self.assertRaises(anthropic.APIStatusError):
                    self.stream(counted)
                self.assertEqual(server.count(), 1)                                          # no SDK retry, no re-asking
                self.assertEqual(b.spent(), 0)
        for record in self.records():
            self.assertEqual((record["reason"], record["basis"], record["cost_usd"]), ("rejected", "rejected", 0.0))
        self.assertEqual([r["provider_status"] for r in self.records()], [400, 429])
        self.assertEqual([r["provider_code"] for r in self.records()], ["invalid_request_error", "rate_limit_error"])
        for path in Path(self.tmp.name).iterdir():
            self.assertNotIn("not allowed for this model", path.read_text(encoding="utf-8"), path.name)

    def test_a_server_error_an_overload_and_a_stream_error_are_charged_at_the_worst_case_and_never_repeated(self):
        for mode, reason in (("overloaded", "server_error"), ("http500", "server_error"), ("stream_error", "server_error")):
            with self.subTest(mode=mode), FakeAnthropic(mode=mode) as server:
                counted, b = make_counted(self.tmp.name, server, ledger=Path(self.tmp.name) / ("spend-%s.jsonl" % mode))
                with self.assertRaises(anthropic.APIStatusError):
                    self.stream(counted)
                self.assertEqual(server.count(), 1)
                self.assertGreater(b.spent(), 0)
                self.assertEqual(budget.Budget(5.0, Path(self.tmp.name) / ("spend-%s.jsonl" % mode), provider="anthropic").spent(), b.spent())
        for record in self.records():
            self.assertEqual((record["reason"], record["basis"]), ("server_error", "worst_case"))
        self.assertEqual([r["provider_code"] for r in self.records()], ["overloaded_error", "api_error", "overloaded_error"])

    def test_a_timeout_is_ambiguous_so_charged_at_the_worst_case(self):
        with FakeAnthropic(delay=1.5) as server:
            counted, b = make_counted(self.tmp.name, server, timeout=0.4)
            with self.assertRaises(Exception) as caught:         # anthropic 2.8.0 lets httpx2.ReadTimeout out of a stream untouched
                self.stream(counted)
            self.assertIn(type(caught.exception).__name__, ("APITimeoutError", "ReadTimeout"))
            self.assertEqual(server.count(), 1)
        self.assertGreater(b.spent(), 0)
        self.assertEqual((self.records()[0]["reason"], self.records()[0]["basis"]), ("timeout", "worst_case"))

    def test_a_stream_that_is_closed_before_the_end_is_charged_at_the_worst_case(self):
        with FakeAnthropic() as server:
            counted, b = make_counted(self.tmp.name, server)
            with counted.messages.stream(model=HAIKU, max_tokens=4000, messages=MESSAGES, output_config=FORMAT) as stream:
                for event in stream:
                    break
        self.assertGreater(b.spent(), 0)
        self.assertEqual(self.records()[0]["basis"], "worst_case")

    def test_the_request_the_sdk_sends_is_the_one_the_caller_built(self):
        with FakeAnthropic() as server:
            counted, _ = make_counted(self.tmp.name, server)
            self.stream(counted)
            body, headers = server.bodies[0], server.headers_seen[0]
        self.assertEqual(sorted(body), ["max_tokens", "messages", "model", "output_config", "stream", "system", "thinking"])
        self.assertIn("sk-ant-test-" + "k" * 20, (headers["x-api-key"], (headers["authorization"] or "").replace("Bearer ", "")))   # SDK 2.x: Bearer
        self.assertTrue(headers["anthropic-version"])

    def test_the_judge_and_attribution_calls_go_through_the_same_counter(self):
        with FakeAnthropic(usage=(1000, 2000, 0, 800)) as server:
            counted, b = make_counted(self.tmp.name, server, limit=5.0, config="judge")
            metered.claude_judge_call(counted, "N01", model=OPUS, max_tokens=5000, messages=MESSAGES, output_config=FORMAT)
            metered.claude_judge_call(counted, "N01", kind="attribution", model=OPUS, max_tokens=500, messages=MESSAGES,
                                      output_config=FORMAT)
        self.assertEqual([r["kind"] for r in self.records()], ["judge", "attribution"])
        self.assertEqual({r["q"] for r in self.records()}, {"N01"})
        self.assertGreater(b.spent(), 0)
        ledger = lines(Path(self.tmp.name) / "spend.jsonl")
        self.assertEqual({r["kind"] for r in ledger if r["ev"] == "reserve"}, {"judge", "attribution"})

    def test_log_lines_carry_only_labels_and_numbers(self):
        import contextlib
        import io
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer), FakeAnthropic(mode="refusal") as server:
            counted, _ = make_counted(self.tmp.name, server)
            self.stream(counted)
        out = buffer.getvalue()
        self.assertIn("chat_eval.call", out)
        self.assertIn("provider=anthropic", out)
        self.assertIn("stop_reason=refusal", out)
        self.assertIn("refusal=general_harms", out)
        self.assertNotIn("CANARY", out)


if __name__ == "__main__":
    unittest.main()
