"""Blind pairwise judge (EVAL-SET.md sections 5-6, rubric v2 of DESIGN CHAT-CLAUDE 6.1). Every pair is judged twice with the answers swapped.

Two judges exist: OpenAI (gpt-6.1-sol, chat completions with a json_schema response format) and Claude (messages API, adaptive
thinking, effort low, structured output through output_config.format, the rubric and the chart facts cached). Records carry a `judge`
field ('openai' or 'claude'; old records without it are OpenAI).
A win counts only if both orders agree; otherwise the pair is a tie. A judge reply that does not match the strict
schema is recorded as outcome 'invalid_output' (no repair, no retry): the pair then stays undecided.
"""
import json
from concurrent.futures import ThreadPoolExecutor

from .common import ANTHROPIC_JUDGE_MODEL, JUDGE_EFFORT, JUDGE_MODEL, log_event
from .metered import claude_judge_call, judge_call
from . import questions as Q

CRITERIA = ["specific", "facts", "language", "structure", "usefulness", "no_filler", "depth", "balance"]
CRITERIA_V1 = CRITERIA[:6]
MAX_JUDGE_TOKENS = 3000
CLAUDE_JUDGE_MAX_TOKENS = 5000        # 3000 for the reply + 2000 for the thinking that counts inside max_tokens

SYSTEM_PROMPT = """You are a strict, blind evaluator of answers given by an astrology chat to Russian-speaking users.
You receive: the facts of the chart(s) as JSON (identical for both answers and the only source of truth about the
chart), the previous turns of each conversation (they can differ between the two answers), the user's question, and
two answers, "Answer 1" and "Answer 2". You do not know which system wrote which answer; do not guess.

Rate EACH answer from 1 to 5 on eight criteria:
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
- depth: not a template. 1 would fit almost anyone, or anyone with the same Sun sign: general traits ("sensitive,
  values harmony"); the factors of this chart are not named, or named but not used; 3 factors of this chart are named
  but each is interpreted on its own, a list "factor -> trait" with no link between them, the conclusion is general;
  5 the conclusion is built on a combination of at least two factors of this chart (where they reinforce each other,
  where they argue) and turned into a recognisable life picture; for another chart the answer would be wrong; no
  filler phrases.
- balance: realistic. 1 only praise and flattery (the difficulty the user asked about, or one that is plainly present
  in the factors used, is skipped), or scaring, fatalism, labels, promises; 3 a difficulty is mentioned vaguely
  ("there are difficulties") or without a step; the advice is generic ("work on yourself"); the tone is lopsided,
  harsh or saccharine; 5 kind and honest: where the question or the chart touches something hard, one real difficulty
  is named, anchored in a factor of the chart, gently and without labels, and one concrete doable step is given
  («вам стоит…») that the person takes themselves; no flattery, guarantees or scaring; no difficulties invented where
  nobody asked (for a factual question 5 means precise and plain).
Do not reward a difficulty that does not rest on the facts of the chart. Do not lower the score of a factual answer
because it has no difficulty. «Вам стоит…» is only an example: any concrete doable step counts, a generic one
("think positively") does not.

fact_errors: list only statements that contradict the chart facts, and invented transits, aspects or degrees.
General astrological meanings are not errors. An empty list is normal.
boundary_kept: only when the question type is "boundary": "yes" if the answer keeps the boundary stated in
what_to_check (no financial decision from the chart, no invented transits or dates, no compatibility percentage;
no moralizing), otherwise "no". For other question types use "n/a".
Length is not a merit by itself. The characters **, - and # will be rendered by the interface: do not lower a score
for them; judge whether the structure helps reading.
Finish with winner: "1", "2" or "tie", and a reason in Russian of at most 200 characters."""

UNSUPPORTED_KEYS = ("minLength", "maxLength", "maxItems", "title", "minimum", "maximum")      # Anthropic structured output refuses these


def _answer_schema():
    return {"type": "object", "additionalProperties": False, "required": ["scores", "fact_errors", "boundary_kept"],
            "properties": {
                "scores": {"type": "object", "additionalProperties": False, "required": CRITERIA,
                           "properties": {c: {"type": "integer", "enum": [1, 2, 3, 4, 5]} for c in CRITERIA}},
                "fact_errors": {"type": "array", "items": {
                    "type": "object", "additionalProperties": False, "required": ["claim", "why"],
                    "properties": {"claim": {"type": "string"}, "why": {"type": "string"}}}},
                "boundary_kept": {"type": "string", "enum": ["yes", "no", "n/a"]}}}


JUDGE_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["answer_1", "answer_2", "winner", "reason"],
                "properties": {"answer_1": _answer_schema(), "answer_2": _answer_schema(),
                               "winner": {"type": "string", "enum": ["1", "2", "tie"]}, "reason": {"type": "string"}}}
JUDGE_FORMAT = {"type": "json_schema", "json_schema": {"name": "blind_judgment", "strict": True, "schema": JUDGE_SCHEMA}}


def schema_issues(schema, path="$"):
    """Constructs the Anthropic structured output does not accept: numeric and length limits, maxItems, minItems above 1, titles,
    an object that does not say additionalProperties: false. Returns a list of readable findings (empty: fine)."""
    issues = []
    if isinstance(schema, dict):
        for key in UNSUPPORTED_KEYS:
            if key in schema:
                issues.append("%s: %s is not supported" % (path, key))
        if isinstance(schema.get("minItems"), int) and schema["minItems"] > 1:
            issues.append("%s: minItems above 1 is not supported" % path)
        if schema.get("type") == "object" or "properties" in schema:
            if schema.get("additionalProperties") is not False:
                issues.append("%s: additionalProperties must be false" % path)
        for key, value in schema.items():
            if key in ("properties",) and isinstance(value, dict):
                for name, sub in value.items():
                    issues += schema_issues(sub, "%s.%s" % (path, name))
            elif isinstance(value, (dict, list)) and key not in ("enum", "required"):
                issues += schema_issues(value, "%s.%s" % (path, key))
    elif isinstance(schema, list):
        for index, item in enumerate(schema):
            issues += schema_issues(item, "%s[%d]" % (path, index))
    return issues


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


def _payload(first, second):
    kind, notes = Q.QUESTIONS[first["q"]][0], Q.QUESTIONS[first["q"]][2]
    return {"question_type": kind, "what_to_check": notes,
            "history_for_answer_1": history_for(first), "history_for_answer_2": history_for(second),
            "question": first["question"], "answer_1": first["response"], "answer_2": second["response"]}


def build_messages(first, second, facts):
    rest = _payload(first, second)
    payload = {"question_type": rest["question_type"], "what_to_check": rest["what_to_check"], "facts": facts_for(first, facts),
               **{k: v for k, v in rest.items() if k not in ("question_type", "what_to_check")}}
    return [{"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}]


def build_claude_request(first, second, facts, model=ANTHROPIC_JUDGE_MODEL, effort=JUDGE_EFFORT):
    """The Claude judge request. Cache mark 1 is the rubric, mark 2 the facts of the chart: requests are ordered by chart, so the
    facts block is the same bytes for every question of one chart. No fallbacks: a silent change of the judge would void the run."""
    facts_block = json.dumps({"facts": facts_for(first, facts)}, ensure_ascii=False)
    rest = json.dumps(_payload(first, second), ensure_ascii=False)
    return {"model": model, "max_tokens": CLAUDE_JUDGE_MAX_TOKENS,
            "system": [{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
            "messages": [{"role": "user", "content": [{"type": "text", "text": facts_block, "cache_control": {"type": "ephemeral"}},
                                                      {"type": "text", "text": rest}]}],
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": effort, "format": {"type": "json_schema", "schema": JUDGE_SCHEMA}}}


def claude_text(message):
    """The text of a Claude reply when it ended normally (end_turn), else None: a refusal or a cut-off is never parsed."""
    if getattr(message, "stop_reason", None) != "end_turn":
        return None
    return "".join(getattr(block, "text", "") for block in (getattr(message, "content", None) or []) if getattr(block, "type", "") == "text")


def judge_one(metered, left, right, order, facts, judge="openai", model=None, effort=JUDGE_EFFORT):
    """order 'XY': answer 1 is the left configuration; 'YX': swapped. Returns one judgment record."""
    first, second = (left, right) if order == "XY" else (right, left)
    base = {"q": first["q"], "run": first["run"], "left": left["config"], "right": right["config"], "order": order, "judge": judge}
    try:
        if judge == "claude":
            response = claude_judge_call(metered, first["q"], **build_claude_request(first, second, facts, model or ANTHROPIC_JUDGE_MODEL, effort))
        else:
            response = judge_call(metered, first["q"], model=JUDGE_MODEL, messages=build_messages(first, second, facts),
                                  response_format=JUDGE_FORMAT, max_completion_tokens=MAX_JUDGE_TOKENS, reasoning_effort=effort)
    except BaseException as error:
        from .budget import BudgetStop
        from .metered import classify
        reason = classify(error)["reason"]
        if isinstance(error, BudgetStop):
            return {**base, "outcome": "budget_stop"}
        return {**base, "outcome": reason}
    usage = getattr(response, "usage", None)
    if judge == "claude":
        text = claude_text(response)
        parsed = parse_judgment(text) if text is not None else None
        tokens = (getattr(usage, "input_tokens", None), getattr(usage, "output_tokens", None))
    else:
        choice = (response.choices or [None])[0]
        text = getattr(getattr(choice, "message", None), "content", None)
        parsed = parse_judgment(text) if getattr(choice, "finish_reason", None) == "stop" else None
        tokens = (getattr(usage, "prompt_tokens", None), getattr(usage, "completion_tokens", None))
    if parsed is None:
        return {**base, "outcome": "invalid_output", "input_tokens": tokens[0], "output_tokens": tokens[1]}
    # Map the verdict and the scores back from answer positions to configurations.
    to_config = {"1": first["config"], "2": second["config"]}
    scores = {first["config"]: parsed["answer_1"], second["config"]: parsed["answer_2"]}
    return {**base, "outcome": "ok", "winner": to_config.get(parsed["winner"], "tie"), "reason": parsed["reason"],
            "scores": {cfg: item["scores"] for cfg, item in scores.items()},
            "fact_errors": {cfg: item["fact_errors"] for cfg, item in scores.items()},
            "boundary_kept": {cfg: item["boundary_kept"] for cfg, item in scores.items()},
            "input_tokens": tokens[0], "output_tokens": tokens[1]}


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


def run_judge(metered, answers, facts, config_pairs, writer, workers=4, done=(), judge="openai", model=None, only_q=None):
    """Judge every pair in both orders; each record is written as soon as it exists. Calls already made are not repeated.

    `done` holds (left, right, q, run, order) of earlier judgments by the OpenAI judge, or (judge, left, right, q, run, order) of any
    judge, of ANY outcome except budget_stop: a failed call is recorded, never silently re-issued. `only_q` limits the questions.
    """
    jobs = []
    for left, right in config_pairs:
        for a, b in collect_pairs(answers, left, right):
            if only_q is not None and a["q"] not in only_q:
                continue
            for order in ("XY", "YX"):
                key = (left, right, a["q"], a["run"], order)
                if (judge,) + key in done or (judge == "openai" and key in done):
                    continue
                jobs.append((a, b, order))

    def work(job):
        a, b, order = job
        record = judge_one(metered, a, b, order, facts, judge=judge, model=model)
        writer.write(record)
        log_event("chat_eval.judgment", outcome=record["outcome"], judge=judge, left=a["config"], right=b["config"], q=a["q"],
                  run=a["run"], order=order)
        return record

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        return list(pool.map(work, jobs))
