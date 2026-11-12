"""Metrics, thresholds, the report (report.md, metrics.json) and the owner's blind pages from a run directory."""
import json
from collections import Counter, defaultdict
from pathlib import Path

from .budget import cost_usd, price_for
from .common import CONFIGS, JUDGE_MODEL
from .facts import ChartFacts, check_answer, is_russian
from .judge import CRITERIA, collect_pairs, pair_verdict
from .page import assign_sides, build_page, chart_label
from .textmetrics import filler, markup_flags, median_or_none, same_start_share, shape
from . import questions as Q

GENERATION_OUTPUT_TYPICAL, SUMMARY_OUTPUT_TYPICAL, JUDGE_OUTPUT_TYPICAL = 700, 450, 1500


def read_jsonl(path):
    path = Path(path)
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def mean(values):
    values = [v for v in values if v is not None]
    return sum(values) / len(values) if values else None


def round_or_none(value, digits=4):
    return None if value is None else round(value, digits)


def load_run(run_dir):
    run_dir = Path(run_dir)
    facts_path = run_dir / "facts.json"
    return {"answers": read_jsonl(run_dir / "answers.jsonl"), "calls": read_jsonl(run_dir / "calls.jsonl"),
            "judgments": read_jsonl(run_dir / "judgments.jsonl"),
            "facts": json.loads(facts_path.read_text(encoding="utf-8")) if facts_path.is_file() else None,
            "metas": {p.stem[5:]: json.loads(p.read_text(encoding="utf-8")) for p in sorted(run_dir.glob("meta-*.json"))}}


def code_metrics(answers, charts):
    """Per configuration: outcomes, markup, shape, filler, facts, money and time; from the saved answers only."""
    result = {}
    for config in sorted({a["config"] for a in answers}):
        mine = [a for a in answers if a["config"] == config and a["q"] != "X01"]
        ok = [a for a in mine if a["outcome"] == "ok"]
        entry = {"answers": len(mine), "outcomes": dict(Counter(a["outcome"] for a in mine))}
        flags = [markup_flags(a["response"]) for a in ok]
        shapes = [shape(a["response"]) for a in ok]
        fillers = [filler(a["response"]) for a in ok]
        russian = [f for a, f in zip(ok, fillers) if is_russian(a["response"])]
        entry.update(
            truncated=sum(a["outcome"] == "truncated" for a in mine),
            old_visible_share=round_or_none(mean([bool(f["old_visible"]) for f in flags])),
            old_visible_kinds=dict(Counter(k for f in flags for k in f["old_visible"])),
            new_unsupported=sum(bool(f["new_unsupported"]) for f in flags),
            new_unsupported_kinds=dict(Counter(k for f in flags for k in f["new_unsupported"])),
            words_median=median_or_none([s["words"] for s in shapes]),
            chars_median=median_or_none([s["chars"] for s in shapes]),
            paragraphs_median=median_or_none([s["paragraphs"] for s in shapes]),
            list_items_mean=round_or_none(mean([s["list_items"] for s in shapes])),
            intro_share=round_or_none(mean([f["intro"] for f in fillers])),
            outro_share=round_or_none(mean([f["outro"] for f in fillers])),
            science_hedges_mean=round_or_none(mean([f["science_hedge"] for f in fillers])),
            chancery_mean=round_or_none(mean([f["chancery"] for f in fillers])),
            emoji_mean=round_or_none(mean([f["emoji"] for f in fillers])),
            latin_share_ru_mean=round_or_none(mean([f["latin_share"] for f in russian])),
            same_start_share=round_or_none(same_start_share([a["response"] for a in ok])),
        )
        confirmed = contradicted = unverifiable = natal_checked = 0
        contradictions = []
        for a in ok:
            if a["subject"] != "natal" or not is_russian(a["response"]):
                continue
            verdict = check_answer(a["response"], ChartFacts(charts[a["chart"].split("+")[0]]))
            natal_checked += 1
            confirmed += verdict["confirmed"]
            contradicted += verdict["contradicted"]
            unverifiable += verdict["unverifiable"]
            contradictions += [{"q": a["q"], "run": a["run"], **c} for c in verdict["contradictions"]]
        checked = confirmed + contradicted
        entry["facts"] = {"answers_checked": natal_checked, "confirmed": confirmed, "contradicted": contradicted,
                          "unverifiable": unverifiable, "contradiction_rate": round_or_none(contradicted / checked) if checked else None,
                          "confirmed_per_answer": round_or_none(confirmed / natal_checked) if natal_checked else None,
                          "contradictions": contradictions}
        entry.update(
            cost_per_answer_usd=round_or_none(mean([a["cost_usd"] for a in ok]), 6),
            cost_total_usd=round(sum(a.get("cost_usd") or 0.0 for a in mine), 6),
            input_tokens_mean=round_or_none(mean([a.get("input_tokens") for a in ok]), 1),
            output_tokens_mean=round_or_none(mean([a.get("output_tokens") for a in ok]), 1),
            ttfb_ms_median=median_or_none([a.get("ttfb_ms") for a in ok]),
            duration_ms_median=median_or_none([a.get("duration_ms") for a in ok]),
            summary_calls=sum(a.get("summary_calls") or 0 for a in mine))
        result[config] = entry
    return result


def judge_metrics(answers, judgments, left, right):
    by_pair = defaultdict(list)
    for j in judgments:
        if (j["left"], j["right"]) == (left, right):
            by_pair[(j["q"], j["run"])].append(j)
    expected = len(collect_pairs(answers, left, right))
    verdicts = Counter(pair_verdict(js) for js in by_pair.values())
    left_wins, right_wins = verdicts.get(left, 0), verdicts.get(right, 0)
    resolved = left_wins + right_wins
    ok = [j for js in by_pair.values() for j in js if j["outcome"] == "ok"]
    means = {cfg: {c: round_or_none(mean([j["scores"][cfg][c] for j in ok])) for c in CRITERIA} for cfg in (left, right)}
    boundary = {cfg: {"yes": 0, "no": 0} for cfg in (left, right)}
    for j in ok:
        if j["q"] in Q.BOUNDARY_IDS:
            for cfg in (left, right):
                if j["boundary_kept"][cfg] in ("yes", "no"):
                    boundary[cfg][j["boundary_kept"][cfg]] += 1
    errors = {cfg: sum(len(j["fact_errors"][cfg]) for j in ok) for cfg in (left, right)}
    return {"left": left, "right": right, "pairs_expected": expected, "pairs_judged": len(by_pair),
            "verdicts": dict(verdicts), "resolved": resolved,
            "right_win_share": round_or_none(right_wins / resolved) if resolved else None,
            "judgments_ok": len(ok), "judgments_invalid": sum(j["outcome"] == "invalid_output" for j in judgments
                                                              if (j["left"], j["right"]) == (left, right)),
            "criteria_means": means,
            "criteria_diff": {c: round_or_none(means[right][c] - means[left][c]) if means[right][c] is not None and means[left][c] is not None else None for c in CRITERIA},
            "fact_errors": errors, "boundary": boundary}


def thresholds(code, judge):
    """DESIGN.md section 4 thresholds for C1 against C0. 'pass', 'fail' or 'no_data' with the number behind it."""
    def verdict(flag):
        return "no_data" if flag is None else ("pass" if flag else "fail")
    rows = []
    c0, c1 = code.get("C0"), code.get("C1")
    if judge:
        resolved, share = judge["resolved"], judge["right_win_share"]
        if not judge["judgments_ok"]:
            wins = "no_data"
        elif resolved < 25:
            wins = "unclear"
        else:
            wins = verdict(share >= 0.65)
        rows.append(("Судья: C1 выигрывает >= 65% решённых пар при >= 25 решённых", "%s решённых из %s пар, доля %s" % (
            resolved, judge["pairs_expected"], share), wins))
        diffs = judge["criteria_diff"]
        known = [d for d in diffs.values() if d is not None]
        rows.append(("Судья: >= 3 из 6 критериев выше на >= 0.5, ни один не ниже более чем на 0.2",
                     json.dumps(diffs, ensure_ascii=False), "no_data" if len(known) < 6 else verdict(
                         sum(d >= 0.5 for d in known) >= 3 and all(d >= -0.2 for d in known))))
        yes_no = judge["boundary"]["C1"]
        total = yes_no["yes"] + yes_no["no"]
        rows.append(("Границы (Г-вопросы): 100% «граница соблюдена»", "%s из %s" % (yes_no["yes"], total),
                     "no_data" if not total else verdict(yes_no["no"] == 0)))
    if c0 and c1:
        r0, r1 = c0["facts"]["contradiction_rate"], c1["facts"]["contradiction_rate"]
        rows.append(("Факты кодом: противоречий C1 <= C0 и <= 5%", "C0 %s, C1 %s" % (r0, r1),
                     "no_data" if r1 is None else verdict((r0 is None or r1 <= r0) and r1 <= 0.05)))
        rows.append(("Обрывы по длине у C1: ноль", str(c1["truncated"]), verdict(c1["truncated"] == 0)))
        rows.append(("Конструкции, которые не отрисует новый интерфейс: ноль", str(c1["new_unsupported"]),
                     verdict(c1["new_unsupported"] == 0)))
        a, b = c0["cost_per_answer_usd"], c1["cost_per_answer_usd"]
        rows.append(("Стоимость ответа C1 <= 2 x C0", "C0 %s, C1 %s" % (a, b),
                     "no_data" if not a or b is None else verdict(b <= 2 * a)))
        t0, t1 = c0["ttfb_ms_median"], c1["ttfb_ms_median"]
        rows.append(("Медиана до первого фрагмента C1 <= C0 + 1 с", "C0 %s мс, C1 %s мс" % (t0, t1),
                     "no_data" if t0 is None or t1 is None else verdict(t1 <= t0 + 1000)))
    return rows


def project_costs(calls):
    """What the same set of calls would cost at the intended models: likely and worst-case (no money is spent in a dry run)."""
    likely = upper = 0.0
    for call in calls:
        if call.get("outcome") == "budget_stop" or "est_input_tokens_upper" not in call:
            continue
        model = call["model_requested"]
        price_in, price_out = price_for(model)
        typical = {"generation": GENERATION_OUTPUT_TYPICAL, "summary": SUMMARY_OUTPUT_TYPICAL}.get(call["kind"], JUDGE_OUTPUT_TYPICAL)
        likely += (call["est_input_tokens_likely"] * price_in + typical * price_out) / 1e6
        upper += (call["est_input_tokens_upper"] * price_in + call["max_output_tokens"] * price_out) / 1e6
    return {"likely_usd": round(likely, 4), "upper_usd": round(upper, 4)}


def spend_by_kind(calls):
    totals = defaultdict(float)
    for call in calls:
        totals[call["kind"]] += call.get("cost_usd") or 0.0
    return {k: round(v, 6) for k, v in totals.items()}


def pick_second_model(answers, judgments):
    """The better of C2/C3 for the model page: more judge wins in C2-against-C3 pairs; no data or equal -> C2 (cheaper)."""
    by_pair = defaultdict(list)
    for j in judgments:
        if (j["left"], j["right"]) == ("C2", "C3"):
            by_pair[(j["q"], j["run"])].append(j)
    verdicts = Counter(pair_verdict(js) for js in by_pair.values())
    present = {a["config"] for a in answers}
    if "C3" not in present:
        return "C2", "only_c2"
    if "C2" not in present:
        return "C3", "only_c3"
    if verdicts.get("C2", 0) + verdicts.get("C3", 0) == 0:
        return "C2", "no_judge_data"
    return ("C3" if verdicts.get("C3", 0) > verdicts.get("C2", 0) else "C2"), "judge_wins"


def build_pages(run_dir, answers, facts, seed, judgments=()):
    """compare.html (C0/C1, first runs, twelve cards) and, if C2/C3 exist, compare-models.html; key.json for both."""
    run_dir = Path(run_dir)
    key, written = {}, []

    def page(name, ids, left, right, title):
        index = {(a["config"], a["q"]): a for a in answers if a["outcome"] == "ok" and a["run"] == 1}
        ids = [q for q in ids if (left, q) in index and (right, q) in index]
        if not ids:
            return
        sides = assign_sides(ids, left, right, seed)
        cards = []
        for q in ids:
            record = index[(left, q)]
            entry = facts["pairs"][record["conv"]] if record["subject"] == "pair" else facts["natal"][record["chart"]]
            cards.append({"id": q, "question": record["question"],
                          "chart_label": chart_label(record["subject"], entry, record["chart"]),
                          "answers": [{"response": index[(sides[q][s], q)]["response"],
                                       "history": [{"question": h["q"], "answer": h["a"]} for h in index[(sides[q][s], q)]["history"]]}
                                      for s in ("1", "2")]})
        (run_dir / name).write_text(build_page(cards, title), encoding="utf-8")
        key[name] = sides
        written.append(name)

    page("compare.html", Q.OWNER_PAGE_IDS, "C0", "C1", "Сравнение ответов Lunaria вслепую: 12 вопросов")
    present = {a["config"] for a in answers}
    extra = {}
    if {"C2", "C3"} & present and "C1" in present:
        best, reason = pick_second_model(answers, judgments)
        page("compare-models.html", Q.MODEL_PAGE_IDS, "C1", best, "Сравнение ответов Lunaria вслепую: выбор модели")
        extra = {"model_page_second": best, "model_page_reason": reason}
    if written:
        (run_dir / "key.json").write_text(json.dumps({"seed": seed, "pages": key, **extra}, ensure_ascii=False, indent=1) + "\n",
                                          encoding="utf-8")
    return written


def render_markdown(run, meta_run, code, judges, thresh, ledger_spent, limit, projection, spent_kinds, pages):
    lines = ["# Отчёт замера CHAT-QUALITY", ""]
    lines.append("Режим: **%s**. Только синтетические карты и вопросы из EVAL-SET.md; в журналах метки и числа, без текстов." %
                 ("настоящий OpenAI" if meta_run["mode"] == "real" else "сухой прогон на подменном OpenAI (деньги не тратятся)"))
    lines += ["", "## Что запускалось", "", "| Конфигурация | Код | Модель | Усилие | SHA кода | Не закоммичено | Ответов |", "|---|---|---|---|---|---|---|"]
    for config, meta in sorted(run["metas"].items()):
        lines.append("| %s | %s | %s | %s | %s | %s | %s |" % (config, meta["code"], meta["model"], meta["effort"] or "—",
                                                              meta["sha"][:10], "да" if meta["dirty"] else "нет", meta["answers"]))
    lines += ["", "SHA инструмента: `%s`. Судья: %s." % (meta_run["tool_sha"], JUDGE_MODEL), "",
              "## Расход", "",
              "- По журналу инструмента: **$%.4f** из лимита $%.2f (по видам вызовов: %s)%s." % (
                  ledger_spent, limit, json.dumps(spent_kinds),
                  "; это условная сумма по числам подмены, деньги не тратились" if meta_run["mode"] != "real" else ""),
              "- Оценка по размерам запросов для тех же вызовов по настоящим ценам: **около $%s**, верхняя граница $%s (оценка, токенизатора нет)." % (
                  projection["likely_usd"], projection["upper_usd"]), ""]
    blocked = sorted({a["config"] for a in run["answers"] if a["q"] == "X01" and a["outcome"] == "blocked"})
    lines += ["## Исходы ответов", "",
              "X01 (известная проблема фильтра, вне оценки): заблокирован фильтром транзитов до вызова модели у %s конфигураций, модель не вызывалась." % len(blocked), "",
              "| Конфигурация | Ответов | Исходы | Обрывы по длине |", "|---|---|---|---|"]
    for config, m in code.items():
        lines.append("| %s | %s | %s | %s |" % (config, m["answers"], json.dumps(m["outcomes"], ensure_ascii=False), m["truncated"]))
    lines += ["", "## Метрики кодом", "",
              "| Метрика | " + " | ".join(code) + " |", "|---|" + "---|" * len(code)]
    rows = [("видна разметка в старом интерфейсе, доля", "old_visible_share"), ("конструкции, не отрисуемые и новым интерфейсом", "new_unsupported"),
            ("слов в ответе, медиана", "words_median"), ("абзацев, медиана", "paragraphs_median"),
            ("пунктов списка в ответе, среднее", "list_items_mean"), ("вступление в начале, доля", "intro_share"),
            ("концовка «если хотите…», доля", "outro_share"), ("оговорок про научность, среднее", "science_hedges_mean"),
            ("канцелярит, среднее на ответ", "chancery_mean"), ("эмодзи, среднее", "emoji_mean"),
            ("одинаковое начало, доля", "same_start_share"), ("стоимость ответа, $", "cost_per_answer_usd"),
            ("токенов на входе, среднее", "input_tokens_mean"), ("токенов на выходе, среднее", "output_tokens_mean"),
            ("до первого фрагмента, мс, медиана", "ttfb_ms_median"), ("до конца ответа, мс, медиана", "duration_ms_median")]
    for title, field in rows:
        lines.append("| %s | %s |" % (title, " | ".join(str(m[field]) for m in code.values())))
    lines += ["", "### Факты (код сверяет утверждения о теле, знаке, доме, градусе и аспекте с сохранённой картой)", "",
              "| Конфигурация | Проверено ответов | Подтверждено | Противоречит | Не проверить | Доля противоречий | Подтверждено на ответ |",
              "|---|---|---|---|---|---|---|"]
    for config, m in code.items():
        f = m["facts"]
        lines.append("| %s | %s | %s | %s | %s | %s | %s |" % (config, f["answers_checked"], f["confirmed"], f["contradicted"],
                                                               f["unverifiable"], f["contradiction_rate"], f["confirmed_per_answer"]))
    for config, m in code.items():
        for c in m["facts"]["contradictions"][:15]:
            lines.append("- %s %s прогон %s, %s: заявлено %s, в карте %s. «%s» — проверить вручную." % (
                config, c["q"], c["run"], c["kind"], c["claimed"], c["expected"], c["sentence"].replace("\n", " ")))
    lines += ["", "## Судья (слепой, пары в двух порядках)", ""]
    if not judges:
        lines.append("Суд не запускался.")
    for judge in judges:
        lines += ["### %s против %s" % (judge["left"], judge["right"]), "",
                  "- Пар ожидалось: %s, оценено: %s, исходы: %s." % (judge["pairs_expected"], judge["pairs_judged"], json.dumps(judge["verdicts"], ensure_ascii=False)),
                  "- Суждений с верной схемой: %s, с неверной схемой (`invalid_output`): %s." % (judge["judgments_ok"], judge["judgments_invalid"]),
                  "- Решённых пар (есть победитель в обоих порядках): %s, доля %s: %s." % (judge["resolved"], judge["right"], judge["right_win_share"]),
                  "- Средние баллы: %s." % json.dumps(judge["criteria_means"], ensure_ascii=False),
                  "- Фактические ошибки по мнению судьи: %s. Г-вопросы «граница соблюдена»: %s." % (json.dumps(judge["fact_errors"]), json.dumps(judge["boundary"])), ""]
    lines += ["## Пороги (DESIGN.md, раздел 4)", "", "| Порог | Значение | Итог |", "|---|---|---|"]
    for title, value, result in thresh:
        lines.append("| %s | %s | %s |" % (title, value, {"pass": "выполнен", "fail": "НЕ выполнен", "unclear": "неясно", "no_data": "нет данных"}[result]))
    lines += ["", "Порог владельца («после» лучше в >= 8 из 12 пар и явное согласие) снимается со страницы: %s." % (", ".join(pages) or "страница не построена"),
              "", "## Ограничения", "",
              "- Токены и стоимость до прогона — оценка; в журнале стоят данные провайдера, где они есть.",
              "- Проверка фактов кодом ловит только простые утверждения (тело в знаке, доме, градусе, аспект двух тел); остальное смотрит судья и владелец.",
              "- Судья — модель, заменитель вкуса пользователей; владелец — один человек."]
    return "\n".join(lines) + "\n"


def build_report(run_dir, seed, ledger_path, limit_usd, charts, pairs_spec):
    """Writes metrics.json, report.md and the pages. pairs_spec: [(left, right), ...] for the judge summary."""
    run = load_run(run_dir)
    code = code_metrics(run["answers"], charts)
    judges = [judge_metrics(run["answers"], run["judgments"], a, b) for a, b in pairs_spec
              if any((j["left"], j["right"]) == (a, b) for j in run["judgments"])]
    main_judge = next((j for j in judges if (j["left"], j["right"]) == ("C0", "C1")), None)
    thresh = thresholds(code, main_judge)
    from .budget import Budget
    spent = Budget(limit_usd, ledger_path).spent()
    projection, kinds = project_costs(run["calls"]), spend_by_kind(run["calls"])
    pages = build_pages(run_dir, run["answers"], run["facts"], seed, run["judgments"]) if run["facts"] else []
    mode = next(iter(run["metas"].values()))["mode"] if run["metas"] else "dry"
    tool = next(iter(run["metas"].values()))["tool_sha"] if run["metas"] else "unknown"
    metrics = {"mode": mode, "tool_sha": tool, "spent_usd": round(spent, 6), "limit_usd": limit_usd, "projection": projection,
               "spend_by_kind": kinds, "code": code, "judges": judges,
               "thresholds": [{"name": n, "value": v, "result": r} for n, v, r in thresh], "pages": pages}
    Path(run_dir, "metrics.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    Path(run_dir, "report.md").write_text(render_markdown(run, {"mode": mode, "tool_sha": tool}, code, judges, thresh, spent,
                                                          limit_usd, projection, kinds, pages), encoding="utf-8")
    return metrics
