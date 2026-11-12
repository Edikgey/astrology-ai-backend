"""Shared constants, closed-vocabulary logging, secret handling. No product imports, no network."""
import hashlib
import math
import os
import re
import subprocess
import sys
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent
# DECISIONS no. 17: the owner's OpenAI project limit is 9 USD, the hard stop of the whole eval is 8.5 (all paid runs together,
# by the shared ledger); the Anthropic account holds 10 USD of credits, the hard stop is 9.5.
BUDGET_DEFAULT_USD = 8.5
OWNER_LIMIT_USD = 9.0
ANTHROPIC_EVAL_LIMIT_USD = 9.5
ANTHROPIC_OWNER_LIMIT_USD = 10.0
OPENAI_URL = "https://api.openai.com/v1"   # the only address a paid run sends the key to (besides a loopback rehearsal)
STAND_OPENAI_URL = "http://127.0.0.1:8101/v1"
ANTHROPIC_URL = "https://api.anthropic.com"
STAND_ANTHROPIC_URL = "http://127.0.0.1:8102"
STUB_KEY = "sk-chat-eval-dry-run-stub"
STUB_ANTHROPIC_KEY = "sk-ant-chat-eval-dry-run-stub"

# USD per 1M tokens (input, output): OpenAI Standard prices read on 11.11.2026 (DESIGN.md section 5).
PRICES = {
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-6-luna": (0.10, 0.50),
    "gpt-5.6-luna": (0.20, 1.20),
    "gpt-6.1-terra": (1.00, 5.00),
    "gpt-6.1-sol": (2.00, 10.00),
    # Anthropic (docs pricing read on 12.11.2026, DESIGN CHAT-CLAUDE 5.2): input, output.
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-opus-5-5": (4.00, 20.00),
}
# Anthropic prompt cache, USD per 1M tokens: (write, read).
CACHE_PRICES = {
    "claude-haiku-5-5": (0.125, 0.01),
    "claude-sonnet-5-5": (2.50, 0.10),
    "claude-opus-5-5": (5.00, 0.20),
}

# name -> which code, provider, model, reasoning effort (None: the variable is not set at all).
# C0..C4, P0 are the earlier OpenAI-only measurement; G*/H* and P1/P2/PH1/PH2 are the CHAT-CLAUDE one (DESIGN 5.1):
# G2 and G3 differ only by the prompt, G3 and H3 only by the model; the cheap effort is the default.
CONFIGS = {
    "C0": {"code": "base", "provider": "openai", "model": "gpt-4o-mini", "effort": None, "role": "before"},
    "C1": {"code": "candidate", "provider": "openai", "model": "gpt-4o-mini", "effort": None, "role": "after, same model"},
    "C2": {"code": "candidate", "provider": "openai", "model": "gpt-6-luna", "effort": "none", "role": "after, new model"},
    "C3": {"code": "candidate", "provider": "openai", "model": "gpt-6-luna", "effort": "low", "role": "after, new model, reasoning low"},
    "C4": {"code": "candidate", "provider": "openai", "model": "gpt-6.1-terra", "effort": "low", "role": "optional quality ceiling"},
    "G2": {"code": "v2", "provider": "openai", "model": "gpt-6-luna", "effort": "none", "role": "prompt effect, GPT"},
    "G3": {"code": "v3", "provider": "openai", "model": "gpt-6-luna", "effort": "none", "role": "candidate, GPT"},
    "G3x": {"code": "v3", "provider": "openai", "model": "gpt-6-luna", "effort": "low", "role": "effort check, GPT"},
    "H2": {"code": "v2", "provider": "anthropic", "model": "claude-haiku-5-5", "effort": "low", "role": "prompt effect, Haiku"},
    "H3": {"code": "v3", "provider": "anthropic", "model": "claude-haiku-5-5", "effort": "low", "role": "candidate, Haiku"},
    "H3x": {"code": "v3", "provider": "anthropic", "model": "claude-haiku-5-5", "effort": "medium", "role": "effort check, Haiku"},
    "P0": {"code": "base", "provider": "openai", "model": "gpt-6-luna", "effort": None, "role": "probe: base parameters, new model"},
    "P1": {"code": "v3", "provider": "openai", "model": "gpt-6-luna", "effort": "none", "role": "probe: parameters accepted"},
    "P2": {"code": "v3", "provider": "openai", "model": "gpt-6-luna", "effort": "low", "role": "probe: parameters accepted"},
    "PH1": {"code": "v3", "provider": "anthropic", "model": "claude-haiku-5-5", "effort": "low", "role": "probe: parameters accepted"},
    "PH2": {"code": "v3", "provider": "anthropic", "model": "claude-haiku-5-5", "effort": "medium", "role": "probe: parameters accepted"},
}
JUDGE_MODEL = "gpt-6.1-sol"
JUDGE_EFFORT = "low"
ANTHROPIC_JUDGE_MODEL = "claude-opus-5-5"     # the owner may choose claude-sonnet-5-5 instead (DESIGN 6.3)

_SECRETS = set()
_KEY_SHAPE = re.compile(r"sk-[A-Za-z0-9_\-]{8,}")
_LABEL = re.compile(r"^[A-Za-z0-9_.:+\-]{0,48}$")


def provider_of(model):
    return "anthropic" if str(model).startswith("claude-") else "openai"


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
    """A finite number above 0 and not above the owner's OpenAI limit (NaN and inf fail every comparison, so test them first)."""
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and 0 < value <= OWNER_LIMIT_USD


def valid_anthropic_budget(value):
    """Like valid_budget, but 0 is allowed and means 'the owner has not named a sum': every paid Anthropic call then refuses."""
    return (isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
            and 0 <= value <= ANTHROPIC_OWNER_LIMIT_USD)


def client_base_url(*, real, dry_url, real_base_url="", provider="openai"):
    """The explicit provider address of every process: never read from the inherited environment."""
    if real:
        return real_base_url or (ANTHROPIC_URL if provider == "anthropic" else OPENAI_URL)
    return dry_url


KEY_VARIABLES = {"openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}


def default_key_file(provider="openai"):
    name = "%s-eval.env" % provider
    root = find_root()
    return (root / ".secrets" / name) if root else Path.cwd() / ".secrets" / name


class KeyError_(Exception):
    """The key file is unusable. The message is a closed reason, never a value."""


def load_key(path, variable="OPENAI_API_KEY"):
    """Reads one key variable from the owner's file into this process' environment. Returns nothing."""
    path = Path(path)
    if not path.is_file():
        raise KeyError_("key_file_missing")
    value = None
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if line.startswith(variable + "="):
            value = line.split("=", 1)[1].strip().strip('"').strip("'")
    if not value or len(value) < 8:
        raise KeyError_("key_missing_in_file")
    os.environ[variable] = value
    register_secret(value)


def child_env(*, real, base_url=None, extra=None, provider="openai"):
    """Environment for a worker process: nothing inherited from OPENAI_*, ANTHROPIC_*, LUNARIA_CHAT_*, STRIPE_*, LAVA_*;
    only its own provider's key (the real one in a paid run, a stub otherwise) and an explicit address."""
    env = {k: v for k, v in os.environ.items()
           if not k.upper().startswith(("OPENAI_", "ANTHROPIC_", "LUNARIA_CHAT_", "STRIPE_", "LAVA_"))}
    env["PYTHONUTF8"] = "1"
    if provider == "anthropic":
        if real:
            env["ANTHROPIC_API_KEY"] = os.environ["ANTHROPIC_API_KEY"]
            env["ANTHROPIC_BASE_URL"] = base_url or ANTHROPIC_URL
        else:
            env["ANTHROPIC_API_KEY"] = STUB_ANTHROPIC_KEY
            env["ANTHROPIC_BASE_URL"] = base_url or STAND_ANTHROPIC_URL
    elif real:
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
