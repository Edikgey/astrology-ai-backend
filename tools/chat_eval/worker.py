"""One configuration, one process: runs the evaluation conversations through the PRODUCT chat path.

The product code comes from --repo (a checkout: the base commit or the candidate branch). Each conversation turn goes
the way the HTTP route does it: transit guard -> usage.reserve -> ai_stream.chat_events (finalize and release inside).
The database is SQLite in memory (fresh for every run), the charts come from charts.json (saved calculation rows),
the provider client is wrapped by MeteredClient (budget check before the call, usage after it, no retries).
Exit codes: 0 done, 2 refused to start, 3 stopped by the budget, 4 aborted (the provider refuses the key).
"""
import argparse
import importlib
import json
import os
import sys
import time
from pathlib import Path

from .budget import Budget, UnknownModel, price_for
from .common import (CONFIGS, STUB_KEY, TOOL_DIR, child_env, client_base_url, git_state, is_loopback_url, log_event,
                     paid_state_dir, redact, refuse_non_loopback, register_secret, tool_sha, valid_budget)
from .facts import ChartFacts
from .metered import JsonlWriter, MeteredClient
from . import questions as Q

TOOL_ROOT = str(TOOL_DIR.parent.parent)
PARAM_SOURCES = ("OPENAI_PARAMS", "openai_params", "chat_params", "model_params", "current_params", "get_openai_params")


def parse(argv):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--repo", required=True)
    parser.add_argument("--config", required=True, choices=sorted(CONFIGS))
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--ledger", required=True)
    parser.add_argument("--budget-usd", type=float, required=True)
    parser.add_argument("--real", action="store_true")
    parser.add_argument("--base-url", default=None, help="dry mode only: loopback OpenAI stand-in")
    parser.add_argument("--real-base-url", default="", help="rehearsal of the paid path against a loopback server")
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--only", default="", help="comma separated question ids (probes use N01)")
    parser.add_argument("--charts", default=str(TOOL_DIR / "charts.json"))
    parser.add_argument("--params-func", default="", help="module:attribute that returns the product parameters")
    return parser.parse_args(argv)


def product_params(interpretation, override):
    """The parameters the product would send for the model in the environment (differs between base and candidate)."""
    source, names = interpretation, list(PARAM_SOURCES)
    if override:
        module_name, _, attribute = override.partition(":")
        if not module_name or not attribute:
            return None, None
        try:
            source = importlib.import_module(module_name)
        except Exception:
            return None, None
        names = [attribute]
    for name in names:
        value = getattr(source, name, None)
        label = override or name
        if callable(value):
            return dict(value()), label
        if isinstance(value, dict):
            return dict(value), label
    return None, None


def seed_charts(engine, charts):
    """Creates the user, the charts (A, B, C, D and the copy A2 with the 24-message history) and both pairs."""
    from datetime import datetime
    from sqlalchemy.orm import Session
    from database.queries import ChartData, GPTMessage, NatalChart, Relationship, User
    from modules.synastry import build_synastry_snapshot
    ids, rows, rel_ids, snapshots = {}, {}, {}, {}
    with Session(bind=engine, autoflush=False) as db:
        db.add(User(id=1, email="chat-eval@example.test", password_hash="unused"))
        db.flush()
        for key, source in (("A", "A"), ("B", "B"), ("C", "C"), ("D", "D"), ("A2", "A")):
            entry = charts[source]
            fields = dict(entry["chart"])
            fields["birth_utc"] = datetime.fromisoformat(fields["birth_utc"])
            chart = NatalChart(user_id=1, **fields)
            db.add(chart)
            db.flush()
            stored = ChartData(chart_id=chart.id, **entry["data"])
            db.add(stored)
            db.flush()
            ids[key], rows[key] = chart.id, (chart, stored)
        for role, text in Q.SEED_MESSAGES:
            db.add(GPTMessage(chart_id=ids["A2"], role=role, content=text, created_at=datetime.utcnow()))
        db.flush()
        for key, (first, second) in (("P1", ("A", "B")), ("P2", ("C", "D"))):
            labels, speaker = Q.PAIRS[key]["labels"], Q.PAIRS[key]["speaker"]
            snapshot = build_synastry_snapshot(rows[first][0], rows[first][1], rows[second][0], rows[second][1])
            relation = Relationship(user_id=1, chart_a_id=ids[first], chart_b_id=ids[second],
                                    person_a_label=labels[0], person_b_label=labels[1], speaker_person=speaker,
                                    calculation=snapshot, ruleset_version=snapshot["ruleset_version"])
            db.add(relation)
            db.flush()
            rel_ids[key], snapshots[key] = relation.id, snapshot
        db.commit()
    return ids, rel_ids, snapshots


def pair_facts(key, charts, snapshot):
    """Facts of a pair for the judge: both natal summaries and the saved inter-chart aspects (names, orbs)."""
    first, second = {"P1": ("A", "B"), "P2": ("C", "D")}[key]
    people = {"A": ChartFacts(charts[first]), "B": ChartFacts(charts[second])}
    labels = Q.PAIRS[key]["labels"]
    names = {"A": {s: b["name"] for s, b in people["A"].bodies.items()}, "B": {s: b["name"] for s, b in people["B"].bodies.items()}}
    from .facts import ASPECT_SYMBOLS
    return {"people": {"A": {"label": labels[0], **people["A"].for_judge()}, "B": {"label": labels[1], **people["B"].for_judge()}},
            "speaker_person": Q.PAIRS[key]["speaker"],
            "interchart_aspects": [[a["participant_a"], names[a["participant_a"]].get(a["body_a"], a["body_a"]),
                                    ASPECT_SYMBOLS[a["aspect"]][1], a["participant_b"],
                                    names[a["participant_b"]].get(a["body_b"], a["body_b"]), round(a["orb_deg"], 2)]
                                   for a in snapshot["aspects"]],
            "columns": ["person_a", "body_a", "aspect", "person_b", "body_b", "orb_deg"]}


def classify_outcome(event, records):
    """Closed outcome of one question from the provider calls the wrapper saw and the stream's final event."""
    if event is not None and event.get("type") == "done":
        return "ok", None
    for call in records:
        if call.get("outcome") == "budget_stop":
            return "budget_stop", None
    summaries = [c for c in records if c["kind"] == "summary"]
    generations = [c for c in records if c["kind"] == "generation"]
    if not generations and summaries and (summaries[-1].get("outcome") != "ok" or
                                          summaries[-1].get("finish_reason") in ("length", "content_filter")):
        return "summary_failed", (summaries[-1].get("provider_status"))
    last = (generations or summaries or [{}])[-1]
    if last.get("finish_reason") == "length":
        return "truncated", None
    if last.get("finish_reason") == "content_filter":
        return "filtered", None
    if last.get("outcome") == "error":
        return last.get("reason", "unclassified"), last.get("provider_status")
    return "empty", None


def ask_one(ctx, conv_id, subject, chart_key, qid, history, seeded, run_index):
    from fastapi import HTTPException
    from sqlalchemy.orm import Session
    from modules import usage
    from modules.ai_stream import chat_events
    from modules.ai_transit_guard import require_supported_question
    question = Q.question_text(qid)
    metered = ctx["metered"]
    metered.q, metered.records = qid, []
    record = {"run_id": ctx["run_id"], "config": ctx["config"], "run": run_index, "q": qid, "conv": conv_id,
              "type": Q.QUESTIONS[qid][0] if qid in Q.QUESTIONS else "filter_probe", "question": question,
              "subject": subject, "chart": "+".join(chart_key), "seeded_history": seeded,
              "history": [dict(item) for item in history], "model_requested": ctx["model"],
              "params": ctx["params_record"], "repo_sha": ctx["repo"]["sha"], "repo_dirty": ctx["repo"]["dirty"],
              "tool_sha": ctx["tool_sha"]}
    try:
        require_supported_question(question)
    except HTTPException:
        return {**record, "outcome": "blocked", "reason": "transit_guard", "response": None, "cost_usd": 0.0}
    rel = {"relationship_id": ctx["rel_ids"][conv_id]} if subject == "pair" else {}
    chart_id = None if subject == "pair" else ctx["chart_ids"][conv_id if conv_id == "A2" else chart_key[0]]
    started = time.monotonic()
    with Session(bind=ctx["engine"], autoflush=False) as db:
        reservation = usage.reserve(db, 1, chart_id, **rel)
    events = chat_events(ctx["engine"], 1, chart_id, question, reservation, metered, dict(ctx["params"]), **rel)
    first_delta_ms, final, text_parts = None, None, []
    try:
        for event in events:
            if event["type"] == "delta":
                if first_delta_ms is None:
                    first_delta_ms = int((time.monotonic() - started) * 1000)
                text_parts.append(event["text"])
            elif event["type"] in ("done", "error"):
                final = event
    finally:
        events.close()
    total_ms = int((time.monotonic() - started) * 1000)
    outcome, status = classify_outcome(final, metered.records)
    generation = next((c for c in metered.records if c["kind"] == "generation" and c.get("outcome") == "ok"), {})
    summaries = [c for c in metered.records if c["kind"] == "summary"]
    out = {**record, "outcome": outcome, "reason": outcome, "provider_status": status,
           "response": final["response"] if outcome == "ok" else None,
           "partial_text": None if outcome == "ok" else "".join(text_parts) or None,
           "follow_up_count": len(final.get("follow_up_suggestions") or []) if outcome == "ok" else None,
           "finish_reason": generation.get("finish_reason"), "model_returned": generation.get("model_returned"),
           "input_tokens": generation.get("input_tokens"), "output_tokens": generation.get("output_tokens"),
           "summary_calls": len(summaries),
           "summary_input_tokens": sum(c.get("input_tokens") or 0 for c in summaries),
           "summary_output_tokens": sum(c.get("output_tokens") or 0 for c in summaries),
           "cost_usd": round(sum(c.get("cost_usd") or 0.0 for c in metered.records), 8),
           "ttfb_ms": first_delta_ms, "duration_ms": total_ms,
           "est_input_tokens_likely": generation.get("est_input_tokens_likely"),
           "est_input_tokens_upper": generation.get("est_input_tokens_upper")}
    return out


def run_once(ctx, run_index, only):
    from sqlalchemy import create_engine, event
    from sqlalchemy.pool import StaticPool
    from database.connection import Base
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    event.listen(engine, "connect", lambda connection, _: connection.execute("PRAGMA foreign_keys=ON"))
    Base.metadata.create_all(engine)
    ctx["engine"] = engine
    ctx["chart_ids"], ctx["rel_ids"], snapshots = seed_charts(engine, ctx["charts"])
    facts_path = ctx["out_dir"] / "facts.json"
    if not facts_path.exists():
        facts = {"natal": {k: ChartFacts(v).for_judge() for k, v in ctx["charts"].items()},
                 "pairs": {k: pair_facts(k, ctx["charts"], snapshots[k]) for k in snapshots}}
        facts_path.write_text(json.dumps(facts, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    auth_failures = 0
    for conv_id, subject, chart_key, qids in Q.CONVERSATIONS:
        history = []
        for qid in qids:
            if only and qid not in only:
                continue
            record = ask_one(ctx, conv_id, subject, chart_key, qid, history, conv_id == "A2", run_index)
            ctx["writer"].write(record)
            log_event("chat_eval.answer", outcome=record["outcome"], config=ctx["config"], run=run_index, q=qid,
                      input_tokens=record.get("input_tokens"), output_tokens=record.get("output_tokens"),
                      cost_usd=record.get("cost_usd"), duration_ms=record.get("duration_ms"),
                      ttfb_ms=record.get("ttfb_ms"))
            ctx["answers"] += 1
            if record["outcome"] == "ok":
                history.append({"q": record["question"], "a": record["response"]})
            if record["outcome"] == "budget_stop":
                return 3
            auth_failures = auth_failures + 1 if record.get("provider_status") in (401, 403) else 0
            if auth_failures >= 2:
                return 4
    return 0


def main(argv=None):
    args = parse(argv)
    config = CONFIGS[args.config]
    out_dir, repo = Path(args.out_dir).resolve(), Path(args.repo).resolve()
    # Same rules as the CLI, because this process can be started directly: a finite limit in (0, 10] and, for a
    # paid run, the one shared ledger (only a loopback rehearsal may use another).
    if not valid_budget(args.budget_usd):
        log_event("chat_eval.worker", outcome="refused", reason="budget_out_of_range", config=args.config)
        return 2
    if args.real and not args.real_base_url and Path(args.ledger).resolve() != (paid_state_dir() / "spend.jsonl").resolve():
        log_event("chat_eval.worker", outcome="refused", reason="paid_ledger_must_be_the_shared_one", config=args.config)
        return 2
    out_dir.mkdir(parents=True, exist_ok=True)
    # --- start conditions: a paid run only with a key, a dry run only against this computer -------------------
    if args.real:
        os.environ.pop("OPENAI_BASE_URL", None)
        if args.real_base_url and not is_loopback_url(args.real_base_url):
            log_event("chat_eval.worker", outcome="refused", reason="real_base_url_must_be_loopback", config=args.config)
            return 2
        if not os.environ.get("OPENAI_API_KEY") or os.environ["OPENAI_API_KEY"] == STUB_KEY:
            log_event("chat_eval.worker", outcome="refused", reason="no_key", config=args.config)
            return 2
        register_secret(os.environ["OPENAI_API_KEY"])
    else:
        base_url = args.base_url or os.environ.get("OPENAI_BASE_URL", "")
        if not base_url or not is_loopback_url(base_url):
            log_event("chat_eval.worker", outcome="refused", reason="dry_needs_loopback_url", config=args.config)
            return 2
        os.environ["OPENAI_BASE_URL"], os.environ["OPENAI_API_KEY"] = base_url, STUB_KEY
        refuse_non_loopback()
    try:
        price_for(config["model"])
    except UnknownModel:
        log_event("chat_eval.worker", outcome="refused", reason="unknown_model_price", config=args.config)
        return 2
    charts = json.loads(Path(args.charts).read_text(encoding="utf-8"))
    out_args = (out_dir, Path(args.ledger).resolve())
    # --- the product, from the chosen checkout, with its own environment ---------------------------------------
    os.environ.update({"DATABASE_URL": "sqlite://", "SECRET_KEY": "chat-eval-only", "LUNARIA_FREE_BETA_ENABLED": "true",
                       "OPENAI_MODEL": config["model"]})
    os.environ.pop("OPENAI_REASONING_EFFORT", None)
    if config["effort"] is not None:
        os.environ["OPENAI_REASONING_EFFORT"] = config["effort"]
    import dotenv
    dotenv.load_dotenv = lambda *a, **k: False       # the repository .env is never read
    sys.path[:0] = [str(repo), TOOL_ROOT]
    os.chdir(repo)
    import warnings
    warnings.filterwarnings("ignore")
    from openai import OpenAI
    from modules import interpretation
    from modules.usage import OPENAI_TIMEOUT_SECONDS
    params, source = product_params(interpretation, args.params_func)
    if params is None or params.get("model") != config["model"]:
        log_event("chat_eval.worker", outcome="refused", reason="product_params_unavailable", config=args.config)
        return 2
    client_args = {"timeout": OPENAI_TIMEOUT_SECONDS, "max_retries": 0,
                   "base_url": client_base_url(real=args.real, dry_url=os.environ.get("OPENAI_BASE_URL", ""),
                                               real_base_url=args.real_base_url)}
    budget = Budget(args.budget_usd, out_args[1])
    run_id = out_dir.name
    metered = MeteredClient(OpenAI(**client_args), budget, JsonlWriter(out_dir / "calls.jsonl"), config=args.config)
    ctx = {"run_id": run_id, "config": args.config, "model": config["model"], "params": params,
           "params_record": {"source": source, **{k: v for k, v in params.items() if isinstance(v, (str, int, float, bool))}},
           "metered": metered, "writer": JsonlWriter(out_dir / "answers.jsonl"), "charts": charts, "out_dir": out_dir,
           "repo": git_state(repo), "tool_sha": tool_sha(), "answers": 0}
    only = {x.strip() for x in args.only.split(",") if x.strip()}
    log_event("chat_eval.worker", outcome="start", config=args.config, mode="real" if args.real else "dry",
              model=config["model"], runs=args.runs, params_source=source, budget_usd=args.budget_usd)
    code = 0
    for run_index in range(1, args.runs + 1):
        code = run_once(ctx, run_index, only)
        if code:
            break
    meta = {"config": args.config, "code": config["code"], "model": config["model"], "effort": config["effort"],
            "repo": str(repo.name), **ctx["repo"], "tool_sha": ctx["tool_sha"], "mode": "real" if args.real else "dry",
            "params": ctx["params_record"], "runs": args.runs, "answers": ctx["answers"], "exit_code": code}
    (out_dir / ("meta-%s.json" % args.config)).write_text(json.dumps(meta, ensure_ascii=False, indent=1) + "\n",
                                                          encoding="utf-8")
    log_event("chat_eval.worker", outcome={0: "done", 3: "stopped", 4: "aborted"}.get(code, "failed"), config=args.config,
              answers=ctx["answers"], spent_usd=round(budget.spent(), 6),
              reason={3: "budget", 4: "auth"}.get(code, "ok"))
    return code


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except BaseException as error:        # never print the message: it may carry request details
        log_event("chat_eval.worker", outcome="crashed", reason=type(error).__name__)
        sys.exit(1)
