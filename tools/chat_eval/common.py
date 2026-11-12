"""Shared constants, closed-vocabulary logging, secret handling. No product imports, no network."""
import hashlib
import math
import os
import re
import subprocess
import sys
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent
BUDGET_DEFAULT_USD = 9.5          # DECISIONS no. 9: hard stop, inside the $10 the owner allowed
OWNER_LIMIT_USD = 10.0            # DECISIONS no. 8: all paid runs together, by the shared ledger
OPENAI_URL = "https://api.openai.com/v1"   # the only address a paid run sends the key to (besides a loopback rehearsal)
STAND_OPENAI_URL = "http://127.0.0.1:8101/v1"
STUB_KEY = "sk-chat-eval-dry-run-stub"

# USD per 1M tokens (input, output): OpenAI Standard prices read on 11.11.2026 (DESIGN.md section 5).
PRICES = {
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-6-luna": (0.10, 0.50),
    "gpt-5.6-luna": (0.20, 1.20),
    "gpt-6.1-terra": (1.00, 5.00),
    "gpt-6.1-sol": (2.00, 10.00),
}

# name -> which code, model, reasoning effort (None: the variable is not set at all).
CONFIGS = {
    "C0": {"code": "base", "model": "gpt-4o-mini", "effort": None, "role": "before"},
    "C1": {"code": "candidate", "model": "gpt-4o-mini", "effort": None, "role": "after, same model"},
    "C2": {"code": "candidate", "model": "gpt-6-luna", "effort": "none", "role": "after, new model"},
    "C3": {"code": "candidate", "model": "gpt-6-luna", "effort": "low", "role": "after, new model, reasoning low"},
    "C4": {"code": "candidate", "model": "gpt-6.1-terra", "effort": "low", "role": "optional quality ceiling"},
    "P0": {"code": "base", "model": "gpt-6-luna", "effort": None, "role": "probe: base parameters, new model"},
    "P1": {"code": "candidate", "model": "gpt-6-luna", "effort": "none", "role": "probe: candidate parameters"},
    "P2": {"code": "candidate", "model": "gpt-6-luna", "effort": "low", "role": "probe: candidate parameters"},
}
JUDGE_MODEL = "gpt-6.1-sol"
JUDGE_EFFORT = "low"

_SECRETS = set()
_KEY_SHAPE = re.compile(r"sk-[A-Za-z0-9_\-]{8,}")
_LABEL = re.compile(r"^[A-Za-z0-9_.:+\-]{0,48}$")


def register_secret(value):
    if value and len(value) >= 8:
        _SECRETS.add(value)


def redact(text):
    text = str(text)
    for secret in _SECRETS:
        text = text.replace(secret, "[key]")
    return _KEY_SHAPE.sub("[key]", text)


def label(value):
    """A closed-vocabulary token: letters, digits and _.:+- only, otherwise 'invalid'."""
    value = str(value)
    return value if _LABEL.match(value) else "invalid"


def log_event(event, **fields):
    """One finite line: only labels and numbers, never text of a question, an answer or an error."""
    parts = [event]
    for name, value in fields.items():
        if isinstance(value, bool):
            rendered = str(value).lower()
        elif isinstance(value, int):
            rendered = str(value)
        elif isinstance(value, float):
            rendered = "%.6g" % value if math.isfinite(value) else "nan"
        elif value is None:
            rendered = "none"
        else:
            rendered = label(value)
        parts.append("%s=%s" % (name, rendered))
    line = redact(" ".join(parts))
    print(line, flush=True)
    return line


def find_root():
    """The coordination root (holds tasks/ and .secrets/); not a git repository."""
    for parent in TOOL_DIR.parents:
        if (parent / "tasks" / "CHAT-QUALITY").is_dir():
            return parent
    return None


def default_tasks_dir():
    root = find_root()
    return (root / "tasks" / "CHAT-QUALITY") if root else Path.cwd() / "CHAT-QUALITY"


def paid_state_dir():
    """The ONE place of the paid money ledger (spend.jsonl) and run.lock, whatever --run-dir is."""
    return default_tasks_dir() / "eval"


def valid_budget(value):
    """A finite number above 0 and not above the owner's $10 (NaN and inf fail every comparison, so test them first)."""
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and 0 < value <= OWNER_LIMIT_USD


def client_base_url(*, real, dry_url, real_base_url=""):
    """The explicit provider address of every process: never read from the inherited environment."""
    if real:
        return real_base_url or OPENAI_URL
    return dry_url


def default_key_file():
    root = find_root()
    return (root / ".secrets" / "openai-eval.env") if root else Path.cwd() / ".secrets" / "openai-eval.env"


class KeyError_(Exception):
    """The key file is unusable. The message is a closed reason, never a value."""


def load_key(path):
    """Reads OPENAI_API_KEY from the owner's file into this process' environment. Returns nothing."""
    path = Path(path)
    if not path.is_file():
        raise KeyError_("key_file_missing")
    value = None
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if line.startswith("OPENAI_API_KEY="):
            value = line.split("=", 1)[1].strip().strip('"').strip("'")
    if not value or len(value) < 8:
        raise KeyError_("key_missing_in_file")
    os.environ["OPENAI_API_KEY"] = value
    register_secret(value)


def child_env(*, real, base_url=None, extra=None):
    """Environment for a worker process: no inherited OPENAI_* values, the key only in real mode."""
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith(("OPENAI_", "STRIPE_", "LAVA_"))}
    env["PYTHONUTF8"] = "1"
    if real:
        env["OPENAI_API_KEY"] = os.environ["OPENAI_API_KEY"]
    else:
        env["OPENAI_API_KEY"] = STUB_KEY
        env["OPENAI_BASE_URL"] = base_url or STAND_OPENAI_URL
    env.update(extra or {})
    return env


def is_loopback_url(url):
    from urllib.parse import urlsplit
    return urlsplit(url).hostname in ("127.0.0.1", "localhost", "::1")


def git_state(repo, ignore=("tools/chat_eval", "tests/test_chat_eval")):
    """HEAD sha and whether the product part of the checkout has uncommitted changes."""
    def git(*args):
        return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=30)
    head = git("rev-parse", "HEAD")
    sha = head.stdout.strip() if head.returncode == 0 else "unknown"
    status = git("status", "--porcelain", "--untracked-files=all")
    dirty = False
    if status.returncode == 0:
        for line in status.stdout.splitlines():
            path = line[3:].strip().strip('"').replace("\\", "/")
            if not any(path.startswith(prefix) for prefix in ignore):
                dirty = True
    return {"sha": sha, "dirty": dirty}


def tool_sha():
    state = git_state(TOOL_DIR.parent.parent, ignore=())
    return state["sha"] + ("+dirty" if state["dirty"] else "")


def file_digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()[:12]


def python_exe():
    return sys.executable


def refuse_non_loopback():
    """Dry mode proof of '$0': any connection that is not to this computer is refused (and counted by the caller)."""
    import socket
    real_connect, real_connect_ex = socket.socket.connect, socket.socket.connect_ex

    def allowed(address):
        host = address[0] if isinstance(address, tuple) and address else None
        return host is None or host in ("127.0.0.1", "::1", "localhost")

    def connect(self, address):
        if not allowed(address):
            raise ConnectionRefusedError("chat_eval dry mode: only loopback connections")
        return real_connect(self, address)

    def connect_ex(self, address):
        if not allowed(address):
            return 111
        return real_connect_ex(self, address)

    socket.socket.connect, socket.socket.connect_ex = connect, connect_ex
