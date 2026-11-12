"""A thin client wrapper: budget check before every call, usage and timing after it, no retries.

The product writes tokens only for successful answers. This wrapper also sees the calls that failed, so the
ledger counts them (a rejected 4xx request costs nothing; a timeout or 5xx is charged at its worst case).
Exception texts are never kept: only the closed reason, the HTTP status and the provider's short code/param.
"""
import json
import re
import threading
import time

from .budget import BudgetStop, cost_usd, input_tokens_likely, input_tokens_upper
from .common import log_event, redact

_SHORT = re.compile(r"^[A-Za-z0-9_.\-]{1,48}$")


def short(value):
    return value if isinstance(value, str) and _SHORT.match(value) else None


def classify(error):
    """Closed reason + provider fields of an exception, without any message text."""
    if isinstance(error, BudgetStop):
        return {"reason": "budget_stop", "status": None, "code": None, "param": None}
    name = type(error).__name__
    status = getattr(error, "status_code", None)
    if name == "APITimeoutError":
        reason = "timeout"
    elif name == "APIConnectionError":
        reason = "network"
    elif isinstance(status, int):
        reason = "rejected" if 400 <= status < 500 else "server_error"
    else:
        reason = "unclassified"
    return {"reason": reason, "status": status if isinstance(status, int) else None,
            "code": short(getattr(error, "code", None)), "param": short(getattr(error, "param", None))}


def _free_error(info):
    """A request the provider refused before generating anything is not billed."""
    return info["status"] is not None and 400 <= info["status"] < 500 and info["status"] not in (408, 409)


class JsonlWriter:
    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, record):
        line = redact(json.dumps(record, ensure_ascii=False, sort_keys=True))
        with self.lock, open(self.path, "a", encoding="utf-8", newline="\n") as handle:
            handle.write(line + "\n")


def _usage_numbers(usage):
    def number(name):
        value = getattr(usage, name, None)
        return value if type(value) is int and value >= 0 else None
    return number("prompt_tokens"), number("completion_tokens")


class _Namespace:
    def __init__(self, **items):
        self.__dict__.update(items)


class MeteredClient:
    """Duck-types the two entry points the product uses: chat.completions.create and beta.chat.completions.stream."""

    def __init__(self, inner, budget, calls_writer, *, config="-"):
        self.inner, self.budget, self.writer = inner, budget, calls_writer
        self.config = config
        self.q = "-"
        self.records = []              # calls of the current answer; the worker empties it per question
        self.budget_stopped = False
        self.chat = _Namespace(completions=_Completions(self))
        self.beta = _Namespace(chat=_Namespace(completions=_BetaCompletions(self)))

    # --- bookkeeping -------------------------------------------------------------------------------------
    def _begin(self, kind, kwargs, q=None):
        size = len(json.dumps(kwargs.get("messages", []), ensure_ascii=False).encode("utf-8"))
        ceiling = kwargs.get("max_completion_tokens") or kwargs.get("max_tokens") or 4096
        try:
            reservation = self.budget.reserve(kind=kind, model=kwargs["model"], input_bytes=size,
                                              max_output_tokens=ceiling, config=self.config, q=q or self.q)
        except BudgetStop:
            self.budget_stopped = True
            self._record({"kind": kind, "q": q or self.q, "outcome": "budget_stop", "reason": "budget_stop",
                          "model_requested": kwargs["model"], "est_input_tokens_upper": input_tokens_upper(size),
                          "cost_usd": 0.0, "basis": "refused"})
            raise
        return {"kind": kind, "reservation": reservation, "model_requested": kwargs["model"],
                "est_input_tokens_upper": input_tokens_upper(size), "est_input_tokens_likely": input_tokens_likely(size),
                "max_output_tokens": ceiling, "t0": time.monotonic(), "ttfb_ms": None, "q": q or self.q}

    def _record(self, record):
        record = {"config": self.config, "q": self.q, **record}
        self.records.append(record)
        self.writer.write(record)
        log_event("chat_eval.call", kind=record["kind"], config=self.config, q=record["q"],
                  outcome=record.get("outcome", "ok"), reason=record.get("reason", "ok"),
                  input_tokens=record.get("input_tokens"), output_tokens=record.get("output_tokens"),
                  cost_usd=record.get("cost_usd", 0.0), duration_ms=record.get("duration_ms"))

    def _finish(self, call, response=None, error=None):
        duration = int((time.monotonic() - call["t0"]) * 1000)
        base = {k: v for k, v in call.items() if k not in ("reservation", "t0")}
        base.update(duration_ms=duration)
        reservation = call["reservation"]
        if error is not None:
            info = classify(error)
            cost, basis = (0.0, "rejected") if _free_error(info) else (reservation.worst_usd, "worst_case")
            self.budget.settle(reservation, cost, basis, outcome=info["reason"])
            self._record({**base, "outcome": "error", "reason": info["reason"], "provider_status": info["status"],
                          "provider_code": info["code"], "provider_param": info["param"],
                          "cost_usd": cost, "basis": basis})
            return
        choice = (getattr(response, "choices", None) or [None])[0]
        finish = getattr(choice, "finish_reason", None)
        model = getattr(response, "model", None)
        usage_in, usage_out = _usage_numbers(getattr(response, "usage", None))
        if usage_in is None or usage_out is None:
            cost, basis = reservation.worst_usd, "worst_case"
        else:
            try:
                cost = cost_usd(model if isinstance(model, str) else call["model_requested"], usage_in, usage_out)
            except Exception:
                cost = cost_usd(call["model_requested"], usage_in, usage_out)
            basis = "actual"
        self.budget.settle(reservation, cost, basis, outcome="ok")
        self._record({**base, "outcome": "ok", "reason": "ok", "finish_reason": short(finish) or "none",
                      "model_returned": short(model) or "none", "input_tokens": usage_in, "output_tokens": usage_out,
                      "cost_usd": cost, "basis": basis})


class _Completions:
    def __init__(self, owner):
        self.owner = owner

    def create(self, **kwargs):
        return self.call("generation" if kwargs.get("response_format") else "summary", kwargs)

    def call(self, kind, kwargs, q=None):
        owner = self.owner
        call = owner._begin(kind, kwargs, q)
        try:
            response = owner.inner.chat.completions.create(**kwargs)
        except BaseException as error:
            owner._finish(call, error=error)
            raise
        owner._finish(call, response=response)
        return response


class _BetaCompletions:
    def __init__(self, owner):
        self.owner = owner

    def stream(self, **kwargs):
        call = self.owner._begin("generation", kwargs)
        return _StreamManager(self.owner, kwargs, call)


class _StreamManager:
    def __init__(self, owner, kwargs, call):
        self.owner, self.call = owner, call
        self.manager = owner.inner.beta.chat.completions.stream(**kwargs)
        self.final = None
        self.recorded = False

    def __enter__(self):
        self.call["t0"] = time.monotonic()
        try:
            self.stream = self.manager.__enter__()
        except BaseException as error:
            self.recorded = True
            self.owner._finish(self.call, error=error)
            raise
        return _StreamProxy(self)

    def __exit__(self, exc_type, exc, tb):
        try:
            return self.manager.__exit__(exc_type, exc, tb)
        finally:
            if not self.recorded:
                self.recorded = True
                if self.final is not None:
                    self.owner._finish(self.call, response=self.final)
                else:
                    self.owner._finish(self.call, error=exc if exc is not None else RuntimeError("stream not completed"))


class _StreamProxy:
    def __init__(self, manager):
        self.manager = manager

    def __iter__(self):
        for event in self.manager.stream:
            if self.manager.call["ttfb_ms"] is None and getattr(event, "type", "") == "content.delta":
                self.manager.call["ttfb_ms"] = int((time.monotonic() - self.manager.call["t0"]) * 1000)
            yield event

    def get_final_completion(self):
        self.manager.final = self.manager.stream.get_final_completion()
        return self.manager.final

    def __getattr__(self, name):
        return getattr(self.manager.stream, name)


def judge_call(metered, q, **kwargs):
    """Judge requests go through the same budget and ledger (kind 'judge'); no retries."""
    return metered.chat.completions.call("judge", kwargs, q)
