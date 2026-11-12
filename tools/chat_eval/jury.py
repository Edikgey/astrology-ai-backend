"""The decision rules of DESIGN CHAT-CLAUDE 6.4, as pure functions on tables of judgments and answers. Thresholds are written here
BEFORE the real run and are not changed after the results (a change is a note of the coordinator made before the results).

judgment rows: the records of judge.judge_one (`judge` is 'openai' or 'claude'; an old row without it is OpenAI).
A pair of answers is "decided" by one judge when both orders name the same winner; the jury decides a question when both judges
decided it the same way.
"""
import math
from collections import defaultdict

from .judge import pair_verdict
from .textmetrics import markup_flags, median_or_none
from . import questions as Q

EPS = 1e-9
J2_ROBUST_EXCLUDE = ("N01", "N02", "N03", "S1-A")        # conversation A and S1-A: the example of the v2 prompt disturbs them (6.4)
JURY_JUDGES = ("openai", "claude")


def judge_of(row):
    return row.get("judge", "openai")


def pair_verdicts(judgments, left, right, judge, qs=None):
    """{(q, run): verdict} of one judge for the pair of configurations; the verdict is a configuration, 'tie' or 'invalid'."""
    groups = defaultdict(list)
    for row in judgments:
        if judge_of(row) == judge and row["left"] == left and row["right"] == right and (qs is None or row["q"] in qs):
            groups[(row["q"], row["run"])].append(row)
    return {key: pair_verdict(rows) for key, rows in groups.items()}


def _name(key):
    q, run = key
    return q if run == 1 else "%s#%d" % (q, run)


def jury_pairs(judgments, left, right, judges=JURY_JUDGES):
    """{question: winner configuration | 'tie' | 'invalid'}: a winner only when every judge named the same one in both orders."""
    per_judge = [pair_verdicts(judgments, left, right, judge) for judge in judges]
    result = {}
    for key in sorted(set().union(*[set(p) for p in per_judge]) if per_judge else []):
        verdicts = [p.get(key, "invalid") for p in per_judge]
        first = verdicts[0]
        if first in (left, right) and all(v == first for v in verdicts):
            result[_name(key)] = first
        elif any(v in ("invalid", "budget_stop") for v in verdicts):
            result[_name(key)] = "invalid"
        else:
            result[_name(key)] = "tie"
    return result


def _mean_scores(judgments, left, right, judge, criterion, qs=None, exclude=()):
    values = {left: [], right: []}
    for row in judgments:
        if (judge_of(row) == judge and row["left"] == left and row["right"] == right and row["outcome"] == "ok"
                and (qs is None or row["q"] in qs) and row["q"] not in exclude):
            for cfg in (left, right):
                values[cfg].append(row["scores"][cfg][criterion])
    return {cfg: (sum(v) / len(v) if v else None) for cfg, v in values.items()}


def _gap(judgments, left, right, judge, criterion, qs=None, exclude=()):
    """right minus left, or None."""
    means = _mean_scores(judgments, left, right, judge, criterion, qs, exclude)
    return None if means[left] is None or means[right] is None else means[right] - means[left]


def _shares(verdicts, left, right):
    wins_left = sum(v == left for v in verdicts.values())
    wins_right = sum(v == right for v in verdicts.values())
    resolved = wins_left + wins_right
    return {"resolved": resolved, "left_share": round(wins_left / resolved, 4) if resolved else None}


def model_choice(judgments, left, right, judges=JURY_JUDGES):
    """J1: left is the GPT candidate (G*), right the Haiku candidate (H*). Explicit winner X: >= 12 resolved questions, X wins at
    least 2/3 of them and leads by at least 6. 'Deeper' / 'more honest': both judges give X a mean depth / balance higher by >= 0.4.
    Family flag: the OpenAI judge gives G* >= 60% of its own decided pairs and the Claude judge gives H* >= 60% of its own."""
    resolved_map = jury_pairs(judgments, left, right, judges)
    wins = {left: sum(v == left for v in resolved_map.values()), right: sum(v == right for v in resolved_map.values())}
    resolved = wins[left] + wins[right]
    shares = {judge: _shares(pair_verdicts(judgments, left, right, judge), left, right) for judge in judges}
    family_split = False
    if "openai" in shares and "claude" in shares:
        openai_left, claude_left = shares["openai"]["left_share"], shares["claude"]["left_share"]
        family_split = (openai_left is not None and claude_left is not None and openai_left >= 0.6 - EPS and claude_left <= 0.4 + EPS)
    decision = "unclear"
    if not family_split and resolved >= 12:
        top = left if wins[left] >= wins[right] else right
        other = right if top == left else left
        if wins[top] * 3 >= resolved * 2 and wins[top] - wins[other] >= 6:
            decision = top

    def lead(criterion):
        gaps = [_gap(judgments, left, right, judge, criterion) for judge in judges]
        if any(g is None for g in gaps):
            return None
        if all(g >= 0.4 - EPS for g in gaps):
            return right
        if all(g <= -0.4 + EPS for g in gaps):
            return left
        return None
    return {"resolved": resolved, "wins": wins, "decision": decision, "family_split": family_split, "judge_shares": shares,
            "deeper": lead("depth"), "more_honest": lead("balance")}


def prompt_effect(judgments, v2, v3, gates_ok, exclude_qs=()):
    """J2 (OpenAI judge only): v3 stays for a model if it wins >= 60% of the decided questions at >= 12 decided, depth and
    balance are +0.5 or more, facts are not lower by more than 0.3, the boundaries are kept 100%, and the v3 gates G1..G7 hold."""
    verdicts = {k: v for k, v in pair_verdicts(judgments, v2, v3, "openai").items() if k[0] not in exclude_qs}
    wins_v3 = sum(v == v3 for v in verdicts.values())
    resolved = wins_v3 + sum(v == v2 for v in verdicts.values())
    diffs = {c: _gap(judgments, v2, v3, "openai", c, exclude=exclude_qs) for c in ("depth", "balance", "facts")}
    reasons = []
    if resolved < 12:
        reasons.append("resolved")
    if resolved and wins_v3 * 5 < resolved * 3:
        reasons.append("share")
    elif not resolved:
        reasons.append("share")
    if diffs["depth"] is None or diffs["depth"] < 0.5 - EPS:
        reasons.append("depth")
    if diffs["balance"] is None or diffs["balance"] < 0.5 - EPS:
        reasons.append("balance")
    if diffs["facts"] is None or diffs["facts"] < -0.3 - EPS:
        reasons.append("facts")
    broken = [row for row in judgments if judge_of(row) == "openai" and row["left"] == v2 and row["right"] == v3 and row["outcome"] == "ok"
              and row["q"] in Q.BOUNDARY_IDS and row["boundary_kept"].get(v3) == "no" and row["q"] not in exclude_qs]
    if broken:
        reasons.append("boundary")
    if not gates_ok:
        reasons.append("gates")
    return {"keep_v3": not reasons, "reasons": reasons, "resolved": resolved,
            "v3_share": round(wins_v3 / resolved, 4) if resolved else None, "diffs": diffs}


def prompt_effect_robust(judgments, v2, v3, gates_ok):
    """J2 again without conversation A and S1-A; a different outcome is 'unstable' and the coordinator decides."""
    full = prompt_effect(judgments, v2, v3, gates_ok)
    trimmed = prompt_effect(judgments, v2, v3, gates_ok, exclude_qs=J2_ROBUST_EXCLUDE)
    return {**full, "keep_v3_without_a": trimmed["keep_v3"], "stable": full["keep_v3"] == trimmed["keep_v3"], "trimmed": trimmed}


def effort_choice(judgments, cheap, costly, costly_gates_ok, judge, s10=None):
    """J3 on S10 by one judge: the costly effort is chosen only if it wins >= 70% of >= 6 decided questions and passes G1, G2, G9."""
    qs = set(s10 if s10 is not None else Q.S10_IDS)
    verdicts = pair_verdicts(judgments, cheap, costly, judge, qs)
    wins_costly = sum(v == costly for v in verdicts.values())
    resolved = wins_costly + sum(v == cheap for v in verdicts.values())
    share = round(wins_costly / resolved, 4) if resolved else None
    choice, reason = cheap, None
    if not costly_gates_ok:
        reason = "gates"
    elif resolved < 6:
        reason = "resolved"
    elif wins_costly * 10 < resolved * 7:
        reason = "share"
    else:
        choice = costly
    return {"choice": choice, "resolved": resolved, "share": share, "reason": reason}


def percentile(values, fraction):
    values = sorted(v for v in values if v is not None)
    if not values:
        return None
    return values[max(0, math.ceil(fraction * len(values)) - 1)]


def admission(answers, baseline, *, contradiction_rate, baseline_rate, boundary):
    """Gates G1..G9 for one candidate on its answers (X01 does not count), against C0 (`baseline`)."""
    mine = [a for a in answers if a.get("q") != "X01"]
    base = [a for a in baseline if a.get("q") != "X01"]
    ok = [a for a in mine if a["outcome"] == "ok"]
    if not mine:
        return {**{"G%d" % i: False for i in range(1, 10)}, "all": False}
    gates = {
        "G1": not any(a["outcome"] == "truncated" for a in mine),
        "G2": not any(a["outcome"] == "filtered" for a in mine),
        "G3": sum(a["outcome"] not in ("ok", "truncated", "filtered", "blocked") for a in mine) <= 1,
        "G4": contradiction_rate is None or (contradiction_rate <= 0.05 + EPS and
                                             (baseline_rate is None or contradiction_rate <= baseline_rate + 0.01 + EPS)),
        "G5": bool(boundary) and all((c["yes"] + c["no"]) > 0 and c["no"] == 0 for c in boundary.values()),
        "G6": not any(markup_flags(a["response"] or "")["new_unsupported"] for a in ok),
        "G7": bool(ok) and sum(a.get("follow_up_count") == 3 for a in ok) / len(ok) >= 0.9 - EPS,
    }
    cost, base_cost = median_or_none([a.get("cost_usd") for a in ok]), median_or_none([a.get("cost_usd") for a in base if a["outcome"] == "ok"])
    gates["G8"] = cost is not None and base_cost is not None and cost <= 2 * base_cost + 1e-12
    ttfb, base_ttfb = [a.get("ttfb_ms") for a in ok], median_or_none([a.get("ttfb_ms") for a in base if a["outcome"] == "ok"])
    median = median_or_none(ttfb)
    gates["G9"] = (median is not None and base_ttfb is not None and median <= base_ttfb + 2000 + EPS
                   and (percentile(ttfb, 0.9) or 0) <= 10000 + EPS)
    gates["all"] = all(gates[g] for g in ("G%d" % i for i in range(1, 10)))
    return gates


def recommend(g, h, *, admitted, j1_decision, owner, cost, ttfb):
    """The team's recommendation (the owner decides anyway): only among the admitted; a clear J1 winner; otherwise the owner's page
    by a margin of 3 (ties not counted); otherwise the cheaper by median cost, and within 20% the faster to the first word."""
    ok = [cfg for cfg in (g, h) if admitted.get(cfg)]
    if not ok:
        return {"choice": None, "basis": "none_admitted", "owner_disagrees": False}
    if len(ok) == 1:
        return {"choice": ok[0], "basis": "only_admitted", "owner_disagrees": False}
    if j1_decision in (g, h):
        other = h if j1_decision == g else g
        return {"choice": j1_decision, "basis": "j1", "owner_disagrees": bool(owner and owner.get(other, 0) >= 8)}
    if owner and abs(owner.get(g, 0) - owner.get(h, 0)) >= 3:
        return {"choice": g if owner[g] > owner[h] else h, "basis": "owner", "owner_disagrees": False}
    cg, ch = cost.get(g), cost.get(h)
    if cg is not None and ch is not None and cg != ch:
        cheaper, dearer = (g, h) if cg < ch else (h, g)
        low, high = min(cg, ch), max(cg, ch)
        if (high - low) / low >= 0.2 - EPS:
            return {"choice": cheaper, "basis": "cost", "owner_disagrees": False}
    tg, th = ttfb.get(g), ttfb.get(h)
    if tg is not None and th is not None and tg != th:
        return {"choice": g if tg < th else h, "basis": "speed", "owner_disagrees": False}
    cheaper = g if (cg is not None and ch is not None and cg <= ch) else h
    return {"choice": cheaper, "basis": "cost", "owner_disagrees": False}
