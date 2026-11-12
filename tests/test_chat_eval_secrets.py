"""CHAT-CLAUDE hardening: no test of the chat-eval run can read anything under `.secrets/` (CLAUDE.md "Секреты", incident 2026-11-12).

1. The guard itself (tests/chat_eval_secrets_guard.py): blocks opens in this process and in children, proven with probe paths
   that do not exist, so no real key file is ever opened by this test.
2. The tool takes the secrets directory from ONE explicit place (`--secrets-dir`, `args.secrets_dir`); the real default belongs to the
   command-line entry point only. In `prepare` a paid run with no key file and no directory is refused, never silently defaulted.
3. No test prints the whole environment: assertions compare specific variable names only.
"""
import ast
import os
import subprocess
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import chat_eval_secrets_guard as guard
from chat_eval_fake_server import FakeOpenAI
from test_chat_eval_budget import REPO, run_cli
from tools.chat_eval import budget, cli, common

TESTS = Path(__file__).resolve().parent


def real_secrets_probe():
    """A path inside the real secrets directory that is NOT a key file: the guard must raise before the OS is asked."""
    root = common.find_root() or REPO.parent.parent
    return root / ".secrets" / ("guard-probe-%s.env" % uuid4().hex)


def fake_secrets(tmp, openai=True, anthropic=True):
    """A temporary directory (its name is not `.secrets`) with fake keys; returns (directory, openai key, anthropic key)."""
    folder = Path(tmp) / "injected-keys"
    folder.mkdir()
    keys = {"openai": "sk-test-" + uuid4().hex, "anthropic": "sk-ant-test-" + uuid4().hex}
    if openai:
        (folder / "openai-eval.env").write_text("OPENAI_API_KEY=%s\n" % keys["openai"], encoding="utf-8")
    if anthropic:
        (folder / "anthropic-eval.env").write_text("ANTHROPIC_API_KEY=%s\n" % keys["anthropic"], encoding="utf-8")
    return folder, keys["openai"], keys["anthropic"]


class GuardTests(unittest.TestCase):
    """1."""

    def test_the_guard_is_installed_in_this_test_run(self):
        self.assertTrue(guard.is_installed())

    def test_every_chat_eval_test_module_imports_the_guard(self):
        modules = sorted(TESTS.glob("test_chat_eval*.py"))
        self.assertGreaterEqual(len(modules), 8)
        missing = [m.name for m in modules if "import chat_eval_secrets_guard" not in m.read_text(encoding="utf-8")]
        self.assertEqual(missing, [], "these test modules would run without the guard when started alone")

    def test_a_path_with_a_secrets_component_is_recognised_in_every_spelling(self):
        for path in (r"C:\Projects\AstrologyAI\.secrets\openai-eval.env", "C:/x/.SECRETS/a.env", b"C:/x/.secrets/a.env",
                     Path("C:/x/.secrets"), "rel/.secrets/k"):
            self.assertTrue(guard.is_secret_path(path), repr(path))
        for path in ("C:/x/secrets/a.env", "C:/x/.secrets-not/a.env", "C:/x/a.secrets", 7, None):
            self.assertFalse(guard.is_secret_path(path), repr(path))

    def test_open_read_text_and_listing_inside_the_real_secrets_directory_are_refused_before_the_disk_is_asked(self):
        probe = real_secrets_probe()
        self.assertFalse(probe.exists())
        with self.assertRaises(guard.SecretsAccessError):
            open(probe, encoding="utf-8")
        with self.assertRaises(guard.SecretsAccessError):
            probe.read_text(encoding="utf-8")
        with self.assertRaises(guard.SecretsAccessError):
            probe.read_bytes()
        with self.assertRaises(guard.SecretsAccessError):
            os.open(str(probe), os.O_RDONLY)
        with self.assertRaises(guard.SecretsAccessError):
            os.listdir(probe.parent)
        with self.assertRaises(guard.SecretsAccessError):
            probe.write_text("x", encoding="utf-8")

    def test_any_directory_called_dot_secrets_is_refused_not_only_the_owners(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(guard.SecretsAccessError):
                (Path(tmp) / ".secrets" / "k.env").read_text(encoding="utf-8")

    def test_an_ordinary_temporary_key_directory_still_works(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder, openai_key, _ = fake_secrets(tmp)
            self.assertIn(openai_key, (folder / "openai-eval.env").read_text(encoding="utf-8"))

    def test_the_guard_reaches_child_processes_that_the_tests_start(self):
        probe = real_secrets_probe()
        code = ("import sys\n"
                "try:\n    open(sys.argv[1], encoding='utf-8')\n"
                "except Exception as error:\n    print(type(error).__name__)\n"
                "else:\n    print('opened')\n")
        env = {k: v for k, v in os.environ.items() if not k.upper().startswith(("OPENAI_", "ANTHROPIC_"))}   # as run_cli builds it
        result = subprocess.run([sys.executable, "-c", code, str(probe)], env=env, cwd=str(REPO), capture_output=True, text=True, timeout=60)
        self.assertEqual(result.stdout.strip(), "SecretsAccessError", result.stderr[-300:])
        self.assertIn("outcome=blocked", result.stderr)
        self.assertNotIn(str(probe.name), result.stderr)            # the line names the event, never the path


class InjectedSecretsDirectoryTests(unittest.TestCase):
    """2. Same shape as the paid-run tests: Namespace for prepare(), a rehearsal against a loopback fake for the command line."""

    def paid_args(self, tmp, **extra):
        values = dict(real=True, run_dir=str(Path(tmp) / "run"), ledger_dir="", real_base_url="", budget_usd=8.5, key_file=None,
                      base_url=common.STAND_OPENAI_URL, command="judge", judges="claude", configs="",
                      anthropic_key_file=None, anthropic_base_url=common.STAND_ANTHROPIC_URL, anthropic_real_base_url="",
                      anthropic_budget_usd=5.0)
        values.update(extra)
        return Namespace(**values)

    def test_prepare_in_paid_mode_loads_the_anthropic_key_from_the_injected_directory(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {}):
            os.environ.pop("ANTHROPIC_API_KEY", None)
            self.addCleanup(common._SECRETS.clear)
            folder, _, anthropic_key = fake_secrets(tmp)
            cli.prepare(self.paid_args(tmp, secrets_dir=str(folder)))
            # assertTrue on a comparison, never assertEqual: a failure must not print a value that may be a real key
            self.assertTrue(os.environ["ANTHROPIC_API_KEY"] == anthropic_key)

    def test_prepare_in_paid_mode_loads_the_openai_key_from_the_injected_directory(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {}):
            os.environ.pop("OPENAI_API_KEY", None)
            self.addCleanup(common._SECRETS.clear)
            folder, openai_key, _ = fake_secrets(tmp)
            cli.prepare(self.paid_args(tmp, secrets_dir=str(folder), command="run", configs="C0", judges=None))
            self.assertTrue(os.environ["OPENAI_API_KEY"] == openai_key)

    def test_a_missing_key_in_the_injected_directory_is_a_refusal_not_a_fallback_to_the_real_files(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {}):
            os.environ.pop("ANTHROPIC_API_KEY", None)
            folder, _, _ = fake_secrets(tmp, anthropic=False)
            with self.assertRaises(SystemExit) as caught:
                cli.prepare(self.paid_args(tmp, secrets_dir=str(folder)))
            self.assertEqual(caught.exception.code, 2)
            self.assertFalse("ANTHROPIC_API_KEY" in os.environ)

    def test_an_explicit_key_file_still_wins_over_the_directory(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {}):
            os.environ.pop("ANTHROPIC_API_KEY", None)
            self.addCleanup(common._SECRETS.clear)
            folder, _, _ = fake_secrets(tmp)
            explicit = Path(tmp) / "explicit.env"
            explicit_key = "sk-ant-test-" + uuid4().hex
            explicit.write_text("ANTHROPIC_API_KEY=%s\n" % explicit_key, encoding="utf-8")
            cli.prepare(self.paid_args(tmp, secrets_dir=str(folder), anthropic_key_file=str(explicit)))
            self.assertTrue(os.environ["ANTHROPIC_API_KEY"] == explicit_key)

    def test_a_paid_run_without_a_key_file_and_without_a_directory_is_refused_in_prepare_and_never_defaults_to_the_real_one(self):
        for extra in ({}, {"secrets_dir": None}, {"secrets_dir": ""}):
            with self.subTest(extra=extra), tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {}):
                os.environ.pop("ANTHROPIC_API_KEY", None)
                with self.assertRaises(SystemExit) as caught:
                    cli.prepare(self.paid_args(tmp, **extra))
                self.assertEqual(caught.exception.code, 2)
                self.assertFalse("ANTHROPIC_API_KEY" in os.environ)

    def test_the_command_line_takes_the_directory_and_a_paid_rehearsal_uses_its_fake_key(self):
        with tempfile.TemporaryDirectory() as tmp, FakeOpenAI() as server:
            root = Path(tmp)
            folder, openai_key, _ = fake_secrets(root)
            result = run_cli("run", "--real", "--real-base-url", server.url, "--configs", "G2", "--runs", "1", "--only", "N01",
                             "--v2-repo", str(REPO), "--secrets-dir", str(folder), "--ledger-dir", str(root / "ledger"),
                             "--anthropic-budget-usd", "0", "--run-dir", str(root / "r"))
            self.assertEqual(result.returncode, 0, result.stdout[-600:] + result.stderr[-300:])
            self.assertNotIn(openai_key, result.stdout + result.stderr)
            self.assertTrue(budget.spent_by_provider(root / "ledger" / "spend.jsonl")["openai"] >= 0)
            self.assertNotIn("outcome=blocked", result.stderr)


def _flags_environment_dump(node):
    """True for `<assert...>(..., os.environ, ...)`, `print(os.environ)` and `dict(os.environ)` in those calls: a failure would print every variable."""
    def is_environ(value):
        if isinstance(value, ast.Attribute) and value.attr == "environ" and isinstance(value.value, ast.Name) and value.value.id == "os":
            return True
        return (isinstance(value, ast.Call) and isinstance(value.func, ast.Name) and value.func.id in ("dict", "str", "repr", "sorted", "list")
                and any(is_environ(a) for a in value.args))
    if not isinstance(node, ast.Call):
        return False
    name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
    if not (name.startswith("assert") or name in ("print", "fail")):
        return False
    return any(is_environ(a) for a in list(node.args) + [k.value for k in node.keywords])


class NoEnvironmentDumpTests(unittest.TestCase):
    """3."""

    def test_the_detector_sees_the_forms_that_print_the_environment(self):
        for source in ("self.assertNotIn('K', os.environ)", "self.assertIn('K', os.environ)", "print(os.environ)",
                       "self.assertEqual(dict(os.environ), {})", "self.assertEqual(os.environ, {})",
                       "self.assertIn('K', dict(os.environ))"):
            self.assertTrue(_flags_environment_dump(ast.parse(source).body[0].value), source)
        for source in ("self.assertFalse('K' in os.environ)", "self.assertTrue(os.environ['K'] == v)", "os.environ.pop('K', None)",
                       "self.assertEqual(os.environ.get('K'), None)"):
            self.assertFalse(_flags_environment_dump(ast.parse(source).body[0].value), source)

    def test_no_chat_eval_test_passes_the_whole_environment_to_an_assertion_or_a_print(self):
        offenders = []
        for path in sorted(TESTS.glob("test_chat_eval*.py")):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if _flags_environment_dump(node):
                    offenders.append("%s:%d" % (path.name, node.lineno))
        self.assertEqual(offenders, [])

    def test_no_chat_eval_test_compares_a_key_variable_with_assert_equal(self):
        # assertEqual/assertNotEqual print both operands; a variable that may hold a real key is compared with assertTrue(a == b)
        offenders = []
        for path in sorted(TESTS.glob("test_chat_eval*.py")):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in ("assertEqual", "assertNotEqual")
                        and any(isinstance(a, ast.Subscript) and isinstance(a.value, ast.Attribute) and a.value.attr == "environ"
                                and isinstance(a.slice, ast.Constant) and a.slice.value in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY")
                                for a in node.args)):
                    offenders.append("%s:%d" % (path.name, node.lineno))
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
