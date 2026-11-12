"""J4, the anti-template check (DESIGN CHAT-CLAUDE 5.5): can a judge tell which of two characteristics was written for chart X?

The answers to the same question S1 ("describe my character in 3-4 sentences") given for two different charts are shown with every
name of a planet, point, sign, house or aspect, every degree and orb masked. The judge sees the facts of both charts and the two
masked answers and says which answer belongs to chart X, or `cannot_tell`. A template that fits everyone cannot be told apart.
Each configuration gets 4 judgments: two pairs of charts (A, D) and (B, C), each in both orders. The judge of the other family is
used (Claude for C0/G*, OpenAI for H*): a model does not judge its own family. The result is reference data, not a threshold.
"""
import json
import re

from .common import ANTHROPIC_JUDGE_MODEL, JUDGE_EFFORT, JUDGE_MODEL
from .facts import _ASPECT_WORDS, _BODY_PATTERNS, _SIGN_PATTERNS
from .metered import claude_judge_call, judge_call
from . import questions as Q

MASK_TOKEN = "[…]"
PAIRS = (("A", "D"), ("B", "C"))
MAX_TOKENS = 1500

_ORDINALS = "перв|втор|трет|четверт|пят|шест|седьм|восьм|девят|десят|одиннадцат|двенадцат"
_RUSSIAN = (
    [pattern for pattern in _BODY_PATTERNS.values()] + [pattern for pattern in _SIGN_PATTERNS.values()] +
    [pattern for pattern in _ASPECT_WORDS.values()] + [
        r"лилит\w*", r"селен\w*", r"вертекс\w*", r"хирон\w*", r"фортун\w*", r"надир\w*", r"десцендент\w*",
        r"(?:черная|белая)\s+лун\w*", r"лунн\w+\s+узл\w*", r"узл(?:а|у|ы|ов|ам)",
        r"полу\w*квадрат\w*", r"полусекстил\w*", r"квинконс\w*", r"квинтил\w*", r"септил\w*", r"новил\w*",
        r"ретроград\w*", r"стеллиум\w*", r"стелиум\w*", r"орбис\w*",
        # houses: "в 12 доме", "12-м доме", "седьмом доме"
        r"\d{1,2}(?:-?(?:й|м|го|ом|ый|ой|е))?\s+дом\w*", r"(?:%s)\w*\s+дом\w*" % _ORDINALS,
        # "29 градусов"; degrees with the sign and minutes (23°05′, 0.3°, 18′) have their own delimiters, see _FREE
        r"\d+(?:[.,]\d+)?\s*градус\w*",
    ])
_ENGLISH = [
    r"sun", r"moon", r"mercury", r"venus", r"mars", r"jupiter", r"saturn", r"uranus", r"neptune", r"pluto", r"ascendant",
    r"midheaven", r"aries", r"taurus", r"gemini", r"cancer", r"leo", r"virgo", r"libra", r"scorpio", r"sagittarius",
    r"capricorn", r"aquarius", r"pisces", r"conjunction", r"opposition", r"trine", r"square", r"sextile", r"orb",
    r"retrograde", r"stellium", r"\d{1,2}(?:st|nd|rd|th)\s+house",
]
_EDGE = r"(?<![\w])(?:%s)(?![\w])"
_PATTERNS = [re.compile(_EDGE % p) for p in _RUSSIAN + _ENGLISH]
# degrees and minutes carry their own delimiters, so they are matched without word edges
_FREE = [re.compile(p) for p in (r"\d+(?:[.,]\d+)?\s*°(?:\s*\d{1,2}\s*[′'’])?", r"\d{1,2}\s*[′'’]")]


def mask_text(text):
    """Hides names, houses, degrees and orbs. Ordinary words stay. Idempotent. Positions are kept: matching is done on a copy in
    which 'ё' is 'е' and letters are lower-case, which has the same length as the original."""
    if not text:
        return ""
    text = str(text)
    norm = text.lower().replace("ё", "е")
    spans = []
    for pattern in _PATTERNS + _FREE:
        spans += [match.span() for match in pattern.finditer(norm) if match.end() > match.start()]
    if not spans:
        return text
    spans.sort()
    merged = [list(spans[0])]
    for start, end in spans[1:]:
        gap = norm[merged[-1][1]:start]
        if start <= merged[-1][1] or (gap.strip() == "" and "\n" not in gap):
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    out, cursor = [], 0
    for start, end in merged:
        out += [text[cursor:start], MASK_TOKEN]
        cursor = end
    out.append(text[cursor:])
    return "".join(out)


ATTRIBUTION_SYSTEM = """ATTRIBUTION TASK. You are a careful evaluator. Two short characteristics of a person were written by an
astrology chat, each for a DIFFERENT chart: chart X and chart Y. Every name of a planet, point, sign, house or aspect and every
degree is replaced by […]. You see the facts of both charts and the two masked texts, "Answer 1" and "Answer 2". Decide which
answer was written for chart X, judging only by what remains: the life picture, the emphasis, the tension, the tone. If the two
texts could have been written for any chart and nothing in them points to one of the two, answer "cannot_tell"; do not guess.
Reply with answer_for_chart_x: "1", "2" or "cannot_tell"."""

ATTRIBUTION_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["answer_for_chart_x"],
                      "properties": {"answer_for_chart_x": {"type": "string", "enum": ["1", "2", "cannot_tell"]}}}
ATTRIBUTION_FORMAT = {"type": "json_schema", "json_schema": {"name": "attribution", "strict": True, "schema": ATTRIBUTION_SCHEMA}}


def parse_attribution(text):
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict) or set(data) != {"answer_for_chart_x"} or data["answer_for_chart_x"] not in ("1", "2", "cannot_tell"):
        return None
    return data["answer_for_chart_x"]


def attribution_judge_for(config):
    """Claude judges C0 and the GPT family, OpenAI judges the Haiku family: nobody judges its own."""
    return "openai" if str(config).startswith("H") else "claude"


def build_job(config, pair, order, answer_x, answer_y, facts_x, facts_y, mask=True, control=None):
    first, second = (answer_x, answer_y) if order == "XY" else (answer_y, answer_x)
    payload = {"chart_x": facts_x, "chart_y": facts_y, "answer_1": mask_text(first) if mask else first,
               "answer_2": mask_text(second) if mask else second}
    job = {"config": config, "pair": pair, "order": order, "chart_x": pair[0], "chart_y": pair[1],
           "truth": "1" if order == "XY" else "2", "payload": payload,
           "messages": [{"role": "system", "content": ATTRIBUTION_SYSTEM},
                        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}]}
    if control:
        job["control"] = control
    return job


def attribution_jobs(answers, facts, config):
    """Up to four jobs for one configuration from its first-run S1 answers (a pair is dropped if one answer is missing)."""
    index = {a["q"]: a for a in answers if a["config"] == config and a["outcome"] == "ok" and a["run"] == 1 and str(a["q"]).startswith("S1-")}
    jobs = []
    for x, y in PAIRS:
        if "S1-" + x in index and "S1-" + y in index:
            for order in ("XY", "YX"):
                jobs.append(build_job(config, x + y, order, index["S1-" + x]["response"], index["S1-" + y]["response"],
                                      facts["natal"][x], facts["natal"][y]))
    return jobs


def run_job(metered, job, judge="openai", model=None):
    """One attribution call through the counter of its provider; returns the record (verdict None when the reply is unusable)."""
    base = {"config": job["config"], "judge": judge, "pair": job["pair"], "order": job["order"], "truth": job["truth"]}
    if job.get("control"):
        base["control"] = job["control"]
    try:
        if judge == "claude":
            from .judge import claude_text
            response = claude_judge_call(
                metered, "S1", kind="attribution", model=model or ANTHROPIC_JUDGE_MODEL, max_tokens=MAX_TOKENS,
                system=[{"type": "text", "text": ATTRIBUTION_SYSTEM, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": json.dumps(job["payload"], ensure_ascii=False)}],
                thinking={"type": "adaptive"}, output_config={"effort": JUDGE_EFFORT, "format": {"type": "json_schema", "schema": ATTRIBUTION_SCHEMA}})
            text = claude_text(response)
        else:
            response = judge_call(metered, "S1", kind="attribution", model=JUDGE_MODEL, messages=job["messages"],
                                  response_format=ATTRIBUTION_FORMAT, max_completion_tokens=MAX_TOKENS, reasoning_effort=JUDGE_EFFORT)
            choice = (response.choices or [None])[0]
            text = getattr(getattr(choice, "message", None), "content", None) if getattr(choice, "finish_reason", None) == "stop" else None
    except BaseException as error:
        from .budget import BudgetStop
        from .metered import classify
        return {**base, "outcome": "budget_stop" if isinstance(error, BudgetStop) else classify(error)["reason"], "verdict": None}
    verdict = parse_attribution(text)
    return {**base, "outcome": "ok" if verdict is not None else "invalid_output", "verdict": verdict}


def attribution_summary(records):
    """Counts over judgments: correct (the verdict equals the truth; `cannot_tell` is right when nothing can be told), wrong,
    cannot_tell, invalid; share = correct / total."""
    counts = {"total": len(records), "correct": 0, "wrong": 0, "cannot_tell": 0, "invalid": 0}
    for record in records:
        verdict, truth = record.get("verdict"), record.get("truth")
        if verdict not in ("1", "2", "cannot_tell"):
            counts["invalid"] += 1
        elif verdict == truth:
            counts["correct"] += 1
        elif verdict == "cannot_tell":
            counts["cannot_tell"] += 1
        else:
            counts["wrong"] += 1
    counts["share"] = round(counts["correct"] / counts["total"], 4) if counts["total"] else None
    return counts
