"""Test-run guard: no code of the chat-eval test run may open anything under a `.secrets` directory.

Why (incident 2026-11-12): the tool's key loading read the owner's real key files by default, a failing assertion printed the
whole environment and a real key ended up in a test log. The rule (CLAUDE.md "Секреты"): tests and the stand never read `.secrets/`.

How: a process-wide audit hook (sys.addaudithook) raises SecretsAccessError on `open`, `os.listdir`, `os.scandir` and the like when ANY
component of the path is `.secrets`. The audit event fires before the operating system is asked, so a probe path that does not exist
is enough to prove the guard (nobody has to touch a real key file). The hook cannot be removed. It is installed:
  * in this process when a test module imports this module (every tests/test_chat_eval*.py does; the scan test enforces it);
  * in every child process: install() puts tests/chat_eval_guard_site (a sitecustomize.py) on PYTHONPATH, and the tests build the
    children's environment from os.environ.
A blocked attempt prints one finite line to stderr (no path, no value) and raises; it never logs a name or a content.
Fake keys for tests live in a temporary directory whose name is not `.secrets`; the tool is told about it explicitly (--secrets-dir).
"""
import os
import sys
from pathlib import Path

SECRETS_NAME = ".secrets"
SITE_DIR = Path(__file__).resolve().parent / "chat_eval_guard_site"
_FLAG = "_chat_eval_secrets_guard_installed"
_PATH_EVENTS = {"open": 0, "os.listdir": 0, "os.scandir": 0, "os.rename": 0, "os.replace": 0, "os.truncate": 0,
                "shutil.copyfile": 0, "shutil.copytree": 0, "os.chdir": 0, "os.startfile": 0}


class SecretsAccessError(PermissionError):
    """A test run tried to touch the secrets directory."""


def is_secret_path(path):
    """True when the path (str, bytes or PathLike; an int is a file descriptor, not a path) has a `.secrets` component."""
    try:
        text = os.fsdecode(os.fspath(path))
    except (TypeError, ValueError):
        return False
    parts = text.replace("\\", "/").split("/")
    return any(part.casefold() == SECRETS_NAME for part in parts)


def _hook(event, args):
    index = _PATH_EVENTS.get(event)
    if index is None or len(args) <= index:
        return
    targets = [args[index]] + ([args[1]] if event in ("os.rename", "os.replace", "shutil.copyfile") and len(args) > 1 else [])
    if any(is_secret_path(target) for target in targets):
        print("chat_eval.secrets_guard outcome=blocked event=%s" % event, file=sys.stderr, flush=True)
        raise SecretsAccessError("the test run touched the secrets directory (event %s)" % event)


def is_installed():
    return getattr(sys, _FLAG, False)


def install():
    """Idempotent. Hook for this process, sitecustomize path for the children."""
    if not is_installed():
        sys.addaudithook(_hook)
        setattr(sys, _FLAG, True)
    parts = [p for p in os.environ.get("PYTHONPATH", "").split(os.pathsep) if p]
    if str(SITE_DIR) not in parts:
        os.environ["PYTHONPATH"] = os.pathsep.join([str(SITE_DIR)] + parts)


install()
