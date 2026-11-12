"""Spend ledger and the hard budget stop. A call is refused BEFORE it is made, by worst-case estimate.

The ledger is append-only JSONL and is ONE file for both providers; every line names its provider (old lines without the field are
OpenAI). Each provider has its own cap. Every call first writes a 'reserve' line with its worst-case cost; the answer settles it with
the real cost. A reserve that is never settled (crash, ambiguous timeout) keeps counting at its worst case, so a lost reply never
makes the spend look smaller.
"""
import json
import math
import threading
from pathlib import Path
from uuid import uuid4

from .common import CACHE_PRICES, PRICES, provider_of, redact, valid_anthropic_budget, valid_budget

HAIKU_LONG_CONTEXT_TOKENS = 100_000        # above this Haiku 5.5 bills a dearer price grid (DESIGN CHAT-CLAUDE 5.2)


class BudgetStop(Exception):
    """The next call would take the total over the limit. Never retried."""


class BudgetUnset(BudgetStop):
    """The owner has not named an Anthropic sum (cap 0): a paid Anthropic call refuses without the network."""


class UnknownModel(Exception):
    """No price for the model: a paid run refuses rather than guess."""


class LongContextPrice(Exception):
    """Haiku with an input estimate above 100K tokens would cost a different price: refused before the call."""


def _family(model, table):
    if model in table:
        return model
    # Dated snapshots such as gpt-4o-mini-2024-07-18 are priced like their family.
    best = max((name for name in table if str(model).startswith(name + "-")), key=len, default=None)
    if best is None:
        raise UnknownModel(model)
    return best


def price_for(model):
    return PRICES[_family(model, PRICES)]


def cache_prices(model):
    return CACHE_PRICES[_family(model, CACHE_PRICES)]


def cost_usd(model, input_tokens, output_tokens):
    price_in, price_out = price_for(model)
    return (input_tokens * price_in + output_tokens * price_out) / 1_000_000


def anthropic_cost_usd(model, *, input_tokens, cache_write_tokens, cache_read_tokens, output_tokens):
    """Input + cache write + cache read + output, each at its own price (the four fields of `usage`)."""
    price_in, price_out = price_for(model)
    write, read = cache_prices(model)
    return (input_tokens * price_in + cache_write_tokens * write + cache_read_tokens * read + output_tokens * price_out) / 1_000_000


def input_tokens_upper(payload_bytes):
    """No local tokenizer exists: one token per 1.5 UTF-8 bytes is above any real rate for this text (safe upper bound)."""
    return math.ceil(payload_bytes / 1.5)


def input_tokens_likely(payload_bytes):
    """About three bytes per token for mixed Russian text and glyph-heavy JSON: a projection, not a bound."""
    return math.ceil(payload_bytes / 3)


def worst_case_usd(model, input_bytes, max_output_tokens):
    """The most one call can cost: the upper input estimate at the dearest input price plus the whole output ceiling.

    Anthropic: the dearest input price is the cache WRITE price. Haiku above 100K input tokens is refused (another price grid).
    """
    tokens = input_tokens_upper(input_bytes)
    if provider_of(model) == "anthropic":
        if str(model).startswith("claude-haiku") and tokens > HAIKU_LONG_CONTEXT_TOKENS:
            raise LongContextPrice(model)
        price_in, price_out = price_for(model)
        dearest = max(price_in, cache_prices(model)[0])
        return (tokens * dearest + max_output_tokens * price_out) / 1_000_000
    return cost_usd(model, tokens, max_output_tokens)


class Reservation:
    def __init__(self, reservation_id, worst_usd):
        self.id, self.worst_usd = reservation_id, worst_usd


def _rows(path):
    path = Path(path)
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def spent_by_provider(ledger_path):
    """{'openai': usd, 'anthropic': usd} from one ledger; a settled call counts its real cost, an open one its worst case."""
    reserved, settled, owner = {}, {}, {}
    for row in _rows(ledger_path):
        if row["ev"] == "reserve":
            reserved[row["id"]] = row["worst_usd"]
            owner[row["id"]] = row.get("provider", "openai")
        elif row["ev"] == "settle":
            settled[row["id"]] = row["cost_usd"]
    totals = {"openai": 0.0, "anthropic": 0.0}
    for call_id, worst in reserved.items():
        totals[owner[call_id]] = totals.get(owner[call_id], 0.0) + settled.get(call_id, worst)
    return totals


class Budget:
    def __init__(self, limit_usd, ledger_path, provider="openai"):
        if provider == "openai":
            ok = valid_budget(limit_usd)
        elif provider == "anthropic":
            ok = valid_anthropic_budget(limit_usd)
        else:
            raise ValueError("unknown provider")
        if not ok:
            raise ValueError("budget must be a finite number inside the owner limit of its provider")
        self.provider = provider
        self.limit = float(limit_usd)
        self.path = Path(ledger_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.stopped = False

    def _append(self, record):
        with open(self.path, "a", encoding="utf-8", newline="\n") as handle:
            handle.write(redact(json.dumps({**record, "provider": self.provider}, ensure_ascii=False, sort_keys=True)) + "\n")

    def spent(self):
        return spent_by_provider(self.path)[self.provider]

    def reserve(self, *, kind, model, input_bytes, max_output_tokens, config="-", q="-"):
        """Worst case = upper input estimate + the full output ceiling the request allows."""
        if provider_of(model) != self.provider:
            raise ValueError("provider_mismatch")
        worst = worst_case_usd(model, input_bytes, max_output_tokens)
        with self.lock:
            if self.provider == "anthropic" and self.limit == 0:
                raise BudgetUnset()
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
