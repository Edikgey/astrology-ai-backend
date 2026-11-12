"""Blind pairwise judge (EVAL-SET.md sections 5-6). Every pair is judged twice with the answers swapped.

A win counts only if both orders agree; otherwise the pair is a tie. A judge reply that does not match the strict
schema is recorded as outcome 'invalid_output' (no repair, no retry): the pair then stays undecided.
"""
import json
from concurrent.futures import ThreadPoolExecutor

from .common import JUDGE_EFFORT, JUDGE_MODEL, log_event
from .metered import judge_call
from . import questions as Q

CRITERIA = ["specific", "facts", "language", "structure", "usefulness", "no_filler"]
MAX_JUDGE_TOKENS = 3000

SYSTEM_PROMPT = """You are a strict, blind evaluator of answers given by an astrology chat to Russian-speaking users.
You receive: the facts of the chart(s) as JSON (identical for both answers and the only source of truth about the
chart), the previous turns of each conversation (they can differ between the two answers), the user's question, and
two answers, "Answer 1" and "Answer 2". You do not know which system wrote which answer; do not guess.

Rate EACH answer from 1 to 5 on six criteria:
- specific: 1 fits any chart; 3 one real factor, general conclusion; 5 two to four exact factors of THIS chart
  (sign, house, aspect with orb) and a conclusion drawn from them.
- facts: 1 two or more errors or an invention (a transit, a non-existent aspect, a wrong degree); 3 one inaccuracy;
  5 everything agrees with the facts and what is unknown is named unknown.
- language: 1 bureaucratic or calqued Russian, mistakes; 3 smooth but formulaic; 5 plain, exact, human Russian
  without "является" and "данный". For an answer that is in English by the user's request, judge the English.
- structure: 1 wall of text; 3 readable but uneven; 5 short paragraphs, a list or bold text only where they help,
  length suited to the question.
- usefulness: 1 does not answer; 3 answers in general terms; 5 answers directly and gives a clear practical takeaway.
- no_filler: 1 introductions, repeating the question, hedging, "if you want, I can..."; 3 some filler; 5 no
  superfluous sentence.

fact_errors: list only statements that contradict the chart facts, and invented transits, aspects or degrees.
General astrological meanings are not errors. An empty list is normal.
boundary_kept: only when the question type is "boundary": "yes" if the answer keeps the boundary stated in
what_to_check (no financial decision from the chart, no invented transits or dates, no compatibility percentage;
no moralizing), otherwise "no". For other question types use "n/a".
Length is not a merit by itself. The characters **, - and # will be rendered by the interface: do not lower a score
for them; judge whether the structure helps reading.
Finish with winner: "1", "2" or "tie", and a reason in Russian of at most 200 characters."""


def _answer_schema():
    return {"type": "object", "additionalProperties": False, "required": ["scores", "fact_errors", "boundary_kept"],
            "properties": {
                "scores": {"type": "object", "additionalProperties": False, "required": CRITERIA,
                           "properties": {c: {"type": "integer", "enum": [1, 2, 3, 4, 5]} for c in CRITERIA}},
                "fact_errors": {"type": "array", "items": {
                    "type": "object", "additionalProperties": False, "required": ["claim", "why"],
                    "properties": {"claim": {"type": "string"}, "why": {"type": "string"}}}},
                "boundary_kept": {"type": "string", "enum": ["yes", "no", "n/a"]}}}


JUDGE_FORMAT = {"type": "json_schema", "json_schema": {
    "name": "blind_judgment", "strict": True,
    "schema": {"type": "object", "additionalProperties": False, "required": ["answer_1", "answer_2", "winner", "reason"],
               "properties": {"answer_1": _answer_schema(), "answer_2": _answer_schema(),
                              "winner": {"type": "string", "enum": ["1", "2", "tie"]}, "reason": {"type": "string"}}}}}


def parse_judgment(text):
    """The judge reply if it matches the schema exactly, else None. No repair."""
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict) or set(data) != {"answer_1", "answer_2", "winner", "reason"}:
        return None
    if data["winner"] not in ("1", "2", "tie") or not isinstance(data["reason"], str):
        return None
    for key in ("answer_1", "answer_2"):
        item = data[key]
        if not isinstance(item, dict) or set(item) != {"scores", "fact_errors", "boundary_kept"}:
            return None
        scores = item["scores"]
        if (not isinstance(scores, dict) or set(scores) != set(CRITERIA) or
                any(type(scores[c]) is not int or not 1 <= scores[c] <= 5 for c in CRITERIA)):
            return None
        if item["boundary_kept"] not in ("yes", "no", "n/a") or not isinstance(item["fact_errors"], list):
            return None
        for error in item["fact_errors"]:
            if not isinstance(error, dict) or set(error) != {"claim", "why"} or not all(isinstance(v, str) for v in error.values()):
                return None
    data["reason"] = data["reason"][:200]
    return data


def history_for(record):
    turns = []
    if record.get("seeded_history"):
        turns += [{"question": q, "answer": a} for q, a in Q.SEED_PAIRS]
    turns += [{"question": h["q"], "answer": h["a"]} for h in record.get("history") or []]
    return turns


def facts_for(record, facts):
    if record["subject"] == "pair":
        return {"pair": facts["pairs"][record["conv"]]}
    return {"chart": facts["natal"][record["chart"]]}


def build_messages(first, second, facts):
    kind, notes = Q.QUESTIONS[first["q"]][0], Q.QUESTIONS[first["q"]][2]
    payload = {"question_type": kind, "what_to_check": notes, "facts": facts_for(first, facts),
               "history_for_answer_1": history_for(first), "history_for_answer_2": history_for(second),
               "question": first["question"], "answer_1": first["response"], "answer_2": second["response"]}
    return [{"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}]


def judge_one(metered, left, right, order, facts):
    """order 'XY': answer 1 is the left configuration; 'YX': swapped. Returns one judgment record."""
    first, second = (left, right) if order == "XY" else (right, left)
    messages = build_messages(first, second, facts)
    base = {"q": first["q"], "run": first["run"], "left": left["config"], "right": right["config"], "order": order}
    try:
        response = judge_call(metered, first["q"], model=JUDGE_MODEL, messages=messages, response_format=JUDGE_FORMAT,
                              max_completion_tokens=MAX_JUDGE_TOKENS, reasoning_effort=JUDGE_EFFORT)
    except BaseException as error:
        from .budget import BudgetStop
        from .metered import classify
        reason = classify(error)["reason"]
        if isinstance(error, BudgetStop):
            return {**base, "outcome": "budget_stop"}
        return {**base, "outcome": reason}
    usage = getattr(response, "usage", None)
    choice = (response.choices or [None])[0]
    text = getattr(getattr(choice, "message", None), "content", None)
    parsed = parse_judgment(text) if getattr(choice, "finish_reason", None) == "stop" else None
    if parsed is None:
        return {**base, "outcome": "invalid_output", "input_tokens": getattr(usage, "prompt_tokens", None),
                "output_tokens": getattr(usage, "completion_tokens", None)}
    # Map the verdict and the scores back from answer positions to configurations.
    to_config = {"1": first["config"], "2": second["config"]}
    scores = {first["config"]: parsed["answer_1"], second["config"]: parsed["answer_2"]}
    return {**base, "outcome": "ok", "winner": to_config.get(parsed["winner"], "tie"), "reason": parsed["reason"],
            "scores": {cfg: item["scores"] for cfg, item in scores.items()},
            "fact_errors": {cfg: item["fact_errors"] for cfg, item in scores.items()},
            "boundary_kept": {cfg: item["boundary_kept"] for cfg, item in scores.items()},
            "input_tokens": getattr(usage, "prompt_tokens", None), "output_tokens": getattr(usage, "completion_tokens", None)}


def pair_verdict(judgments):
    """One pair, two orders -> 'left' config / 'right' config / 'tie' / 'invalid' / 'budget_stop'."""
    if any(j["outcome"] == "budget_stop" for j in judgments):
        return "budget_stop"
    if len(judgments) != 2 or any(j["outcome"] != "ok" for j in judgments):
        return "invalid"
    winners = {j["winner"] for j in judgments}
    return next(iter(winners)) if len(winners) == 1 and "tie" not in winners else "tie"


def collect_pairs(answers, left, right):
    """Pairs of answers (same question, same run index) that both ended in 'ok'."""
    index = {(a["config"], a["q"], a["run"]): a for a in answers if a["outcome"] == "ok"}
    return [(index[(left, q, run)], index[(right, q, run)]) for (cfg, q, run) in sorted(index)
            if cfg == left and (right, q, run) in index]


def run_judge(metered, answers, facts, config_pairs, writer, workers=4, done=()):
    """Judge every pair in both orders; each record is written as soon as it exists. Calls already made are not repeated.

    `done` holds (left, right, q, run, order) of earlier judgments of ANY outcome except budget_stop: a failed call is
    recorded, never silently re-issued.
    """
    jobs = []
    for left, right in config_pairs:
        for a, b in collect_pairs(answers, left, right):
            jobs += [(a, b, order) for order in ("XY", "YX") if (left, right, a["q"], a["run"], order) not in done]

    def work(job):
        a, b, order = job
        record = judge_one(metered, a, b, order, facts)
        writer.write(record)
        log_event("chat_eval.judgment", outcome=record["outcome"], left=a["config"], right=b["config"], q=a["q"],
                  run=a["run"], order=order)
        return record

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        return list(pool.map(work, jobs))
