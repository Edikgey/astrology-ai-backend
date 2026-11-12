"""Spend ledger and the hard budget stop. A call is refused BEFORE it is made, by worst-case estimate.

The ledger is append-only JSONL. Every call first writes a 'reserve' line with its worst-case cost; the answer
settles it with the real cost. A reserve that is never settled (crash, ambiguous timeout) keeps counting at
its worst case, so a lost reply never makes the spend look smaller.
"""
import json
import math
import threading
from pathlib import Path
from uuid import uuid4

from .common import PRICES, redact


class BudgetStop(Exception):
    """The next call would take the total over the limit. Never retried."""


class UnknownModel(Exception):
    """No price for the model: a paid run refuses rather than guess."""


def price_for(model):
    if model in PRICES:
        return PRICES[model]
    # Dated snapshots such as gpt-4o-mini-2024-07-18 are priced like their family.
    best = max((name for name in PRICES if str(model).startswith(name + "-")), key=len, default=None)
    if best is None:
        raise UnknownModel(model)
    return PRICES[best]


def cost_usd(model, input_tokens, output_tokens):
    price_in, price_out = price_for(model)
    return (input_tokens * price_in + output_tokens * price_out) / 1_000_000


def input_tokens_upper(payload_bytes):
    """No local tokenizer exists: one token per 1.5 UTF-8 bytes is above any real rate for this text (safe upper bound)."""
    return math.ceil(payload_bytes / 1.5)


def input_tokens_likely(payload_bytes):
    """About three bytes per token for mixed Russian text and glyph-heavy JSON: a projection, not a bound."""
    return math.ceil(payload_bytes / 3)


class Reservation:
    def __init__(self, reservation_id, worst_usd):
        self.id, self.worst_usd = reservation_id, worst_usd


class Budget:
    def __init__(self, limit_usd, ledger_path):
        self.limit = float(limit_usd)
        self.path = Path(ledger_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.stopped = False

    def _append(self, record):
        with open(self.path, "a", encoding="utf-8", newline="\n") as handle:
            handle.write(redact(json.dumps(record, ensure_ascii=False, sort_keys=True)) + "\n")

    def spent(self):
        reserved, settled = {}, {}
        if self.path.is_file():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                row = json.loads(line)
                if row["ev"] == "reserve":
                    reserved[row["id"]] = row["worst_usd"]
                elif row["ev"] == "settle":
                    settled[row["id"]] = row["cost_usd"]
        return sum(settled.get(i, worst) for i, worst in reserved.items())

    def reserve(self, *, kind, model, input_bytes, max_output_tokens, config="-", q="-"):
        """Worst case = upper input estimate + the full output ceiling the request allows."""
        worst = cost_usd(model, input_tokens_upper(input_bytes), max_output_tokens)
        with self.lock:
            if self.stopped or self.spent() + worst > self.limit + 1e-12:
                self.stopped = True
                raise BudgetStop()
            reservation = Reservation(uuid4().hex[:12], worst)
            self._append({"ev": "reserve", "id": reservation.id, "kind": kind, "config": config, "q": q,
                          "model": model, "worst_usd": worst})
            return reservation

    def settle(self, reservation, cost, basis, **numbers):
        with self.lock:
            self._append({"ev": "settle", "id": reservation.id, "cost_usd": cost, "basis": basis, **numbers})
