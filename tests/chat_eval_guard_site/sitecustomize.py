"""Loaded by every child Python of a chat-eval test run (PYTHONPATH, set by tests/chat_eval_secrets_guard.py): installs the same
audit hook, so a subprocess of the tool cannot open anything under `.secrets` either."""
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent))
import chat_eval_secrets_guard  # noqa: E402,F401  (installs the hook on import)
