"""Command line: probe, run, judge, report, cycle. Dry by default; a paid run needs --real and the owner's key file.

  python -m tools.chat_eval cycle --base-repo <base checkout> --candidate-repo <candidate checkout>          (dry, earlier measurement)
  python -m tools.chat_eval cycle --claude --base-repo ... --v2-repo ... --v3-repo ...                        (dry, CHAT-CLAUDE)

CHAT-CLAUDE paid stages, in this order (EVAL-TOOL.md of the task): probe, judge --stage controls, run, judge --stage effort,
judge --stage main, report. `cycle --claude` is the same chain in one command and exists for dry runs only.
Output lines are finite events (labels and numbers); texts never reach the console or the logs.
"""
import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from .budget import Budget, spent_by_provider
from .common import (ANTHROPIC_EVAL_LIMIT_USD, ANTHROPIC_JUDGE_MODEL, BUDGET_DEFAULT_USD, CLAUDE_JUDGE_MODELS, CONFIGS, STAND_ANTHROPIC_URL,
                     STAND_OPENAI_URL, STUB_ANTHROPIC_KEY, STUB_KEY, TOOL_DIR, KeyError_, child_env, client_base_url, default_secrets_dir,
                     default_tasks_dir, key_file_for, is_loopback_url, load_key, log_event, paid_state_dir, redact, refuse_non_loopback,
                     valid_anthropic_budget, valid_budget)
from .facts import ChartFacts
from .judge import run_judge
from .metered import JsonlWriter, MeteredAnthropic, MeteredClient
from .report import build_claude_report, build_report, choose_finals, code_metrics, load_run, read_jsonl
from . import attribution, controls, questions as Q

TOOL_ROOT = TOOL_DIR.parent.parent
DEFAULT_RUNS = {"C0": 2, "C1": 2, "C2": 1, "C3": 1, "C4": 1, "P0": 1, "P1": 1, "P2": 1}
V2_CONFIGS = "C0,G2,G3,G3x,H2,H3,H3x"
PROBE_CONFIGS = ("P1", "P2", "PH1", "PH2")
STAGES = ("controls", "effort", "main")
CODE_REPO_ARG = {"base": "base_repo", "candidate": "candidate_repo", "v2": "v2_repo", "v3": "v3_repo"}
EXIT_J0_FAILED = 5


def add_common(parser):
    parser.add_argument("--real", action="store_true", help="paid run with the keys from the owner's files (default: dry, loopback fakes)")
    parser.add_argument("--base-repo", help="checkout with the BASE commit (C0, probe P0)")
    parser.add_argument("--candidate-repo", help="checkout with the candidate branch of the earlier measurement (C1-C4)")
    parser.add_argument("--v2-repo", help="checkout with the 'v2 point' (the Claude provider, the old prompt): G2, H2")
    parser.add_argument("--v3-repo", help="checkout with the 'v3 point' (the new prompt): G3, G3x, H3, H3x, probes P1, P2, PH1, PH2")
    parser.add_argument("--run-dir", help="default: <tasks>/eval-dry (dry) or <tasks>/eval/<run id> (real)")
    parser.add_argument("--budget-usd", type=float, default=BUDGET_DEFAULT_USD, help="OpenAI hard cap of the whole eval: default and highest allowed value 8.5 (the owner project limit is 9)")
    parser.add_argument("--anthropic-budget-usd", type=float, default=ANTHROPIC_EVAL_LIMIT_USD,
                        help="Anthropic hard cap of the whole eval: default and highest allowed value 9.5 (credits 10); 0 means the owner named no sum: no paid call")
    parser.add_argument("--secrets-dir", default=None,
                        help="folder with openai-eval.env and anthropic-eval.env (paid mode; default: the owner's .secrets folder, applied by the command line only)")
    parser.add_argument("--key-file", default=None, help="OpenAI key file (default <secrets-dir>/openai-eval.env)")
    parser.add_argument("--anthropic-key-file", default=None, help="Anthropic key file (default <secrets-dir>/anthropic-eval.env)")
    parser.add_argument("--base-url", default=STAND_OPENAI_URL, help="dry mode: the loopback OpenAI stand-in")
    parser.add_argument("--anthropic-base-url", default=STAND_ANTHROPIC_URL, help="dry mode: the loopback Anthropic stand-in (port 8102)")
    parser.add_argument("--real-base-url", default="",
                        help="rehearsal of the paid OpenAI code path against a LOOPBACK server (tests); never an external address")
    parser.add_argument("--anthropic-real-base-url", default="", help="the same for the paid Anthropic path")
    parser.add_argument("--ledger-dir", default="",
                        help="TESTS ONLY, and only with --real and a loopback rehearsal address: another folder for the paid ledger")
    parser.add_argument("--charts", default=str(TOOL_DIR / "charts.json"))
    parser.add_argument("--seed", default=None, help="seed of the blind shuffle (default: the run directory name)")
    parser.add_argument("--params-func", default="", help="module:attr of the candidate's parameter source, if not auto-detected")
    parser.add_argument("--judges", default=None, help="comma separated: openai,claude (stage defaults: controls and main both, effort claude)")
    parser.add_argument("--claude-judge-model", choices=CLAUDE_JUDGE_MODELS, default=ANTHROPIC_JUDGE_MODEL,
                        help="claude-opus-5-5 (default) or claude-sonnet-5-5; never a candidate model")
    parser.add_argument("--g-final", choices=("G3", "G3x"), default=None,
                        help="the GPT finalist, instead of J3's choice (the report then says J3 was skipped)")
    parser.add_argument("--h-final", choices=("H3", "H3x"), default=None,
                        help="the Haiku finalist, instead of J3's choice (the report then says J3 was skipped)")
    parser.add_argument("--owner-line", default=None, help="the line the owner copied from the page")


def config_list(args):
    raw = getattr(args, "configs", "") or ""
    return [c.strip() for c in raw.split(",") if c.strip()]


def effective_judges(args):
    raw, stage = getattr(args, "judges", None), getattr(args, "stage", None)
    if raw:
        return tuple(x.strip() for x in raw.split(",") if x.strip())
    if stage == "effort":
        return ("claude",)
    if stage in ("controls", "main") or getattr(args, "claude", False):
        return ("openai", "claude")
    return ("openai",)


def _providers(args):
    """The providers a command will call (a paid run asks for a key and a cap only for these)."""
    command = getattr(args, "command", None)
    found = set()
    if command in ("run", "probe", "cycle"):
        configs = config_list(args) or (list(PROBE_CONFIGS) if command == "probe" else
                                        (V2_CONFIGS.split(",") if getattr(args, "claude", False) else ["C0", "C1"]))
        found |= {CONFIGS[c]["provider"] for c in configs if c in CONFIGS}
    if command in ("judge", "cycle"):
        found |= {"anthropic" if j == "claude" else "openai" for j in effective_judges(args)}
    if command is None:
        found.add("openai")
    return found


def needs_openai(args):
    return "openai" in _providers(args)


def needs_anthropic(args):
    return "anthropic" in _providers(args)


def settle_paths(args):
    tasks = default_tasks_dir()
    if args.run_dir:
        run_dir = Path(args.run_dir).resolve()
    elif args.real:
        run_dir = tasks / "eval" / datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    else:
        run_dir = tasks / "eval-dry"
    if args.real:
        # One ledger and one lock for every paid run, whatever --run-dir says (the limit is shared by all of them).
        ledger = (Path(args.ledger_dir).resolve() if args.ledger_dir else paid_state_dir()) / "spend.jsonl"
    else:
        ledger = run_dir / "spend.jsonl"
    return run_dir, ledger


def refuse(reason, **fields):
    log_event("chat_eval.cli", outcome="refused", reason=reason, **fields)
    raise SystemExit(2)


def prepare(args):
    """Start conditions for any command that may call a provider. Returns (run_dir, ledger) or exits with a reason."""
    if not valid_budget(args.budget_usd):
        refuse("budget_out_of_range", provider="openai", limit_usd=BUDGET_DEFAULT_USD)
    anthropic_cap = getattr(args, "anthropic_budget_usd", ANTHROPIC_EVAL_LIMIT_USD)
    openai_rehearsal, anthropic_rehearsal = getattr(args, "real_base_url", ""), getattr(args, "anthropic_real_base_url", "")
    if args.ledger_dir and not (args.real and (openai_rehearsal or anthropic_rehearsal)):
        refuse("ledger_dir_only_for_rehearsal")
    for url in (openai_rehearsal, anthropic_rehearsal):
        if url and not is_loopback_url(url):
            refuse("real_base_url_must_be_loopback")
    need_openai, need_anthropic = needs_openai(args), needs_anthropic(args)
    secrets_dir = getattr(args, "secrets_dir", None)     # explicit; the real default is applied by main() only
    if args.real:
        if need_anthropic:                       # the cap is checked before any key file is looked for
            if not valid_anthropic_budget(anthropic_cap):
                refuse("budget_out_of_range", provider="anthropic", limit_usd=ANTHROPIC_EVAL_LIMIT_USD)
            if anthropic_cap == 0:
                refuse("anthropic_budget_unset")
        # Nothing inherited from either provider survives: not an address (it would receive the key), not ANTHROPIC_AUTH_TOKEN,
        # OPENAI_ORG_ID or OPENAI_PROJECT_ID (the SDKs read them on their own and would send a request to another account or
        # project, past the limit). Only the key loaded below stays; the children are built from this environment.
        for name in [n for n in os.environ if n.upper().startswith(("OPENAI_", "ANTHROPIC_"))]:
            del os.environ[name]
        try:
            if need_openai:
                load_key(key_file_for("openai", getattr(args, "key_file", None), secrets_dir), "OPENAI_API_KEY")
            if need_anthropic:
                load_key(key_file_for("anthropic", getattr(args, "anthropic_key_file", None), secrets_dir), "ANTHROPIC_API_KEY")
        except KeyError_ as error:
            refuse(str(error))
    else:
        if need_openai:
            if not is_loopback_url(args.base_url):
                refuse("dry_needs_loopback_url")
            os.environ["OPENAI_API_KEY"] = STUB_KEY
        if need_anthropic:
            if not is_loopback_url(getattr(args, "anthropic_base_url", STAND_ANTHROPIC_URL)):
                refuse("dry_needs_loopback_url")
            os.environ["ANTHROPIC_API_KEY"] = STUB_ANTHROPIC_KEY
    return settle_paths(args)


class RunLock:
    """One paid run at a time: the ledger sums are only exact when calls do not overlap."""

    def __init__(self, ledger, enabled):
        self.path, self.enabled = Path(ledger).with_name("run.lock"), enabled

    def __enter__(self):
        if self.enabled:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.close(os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            except FileExistsError:
                log_event("chat_eval.cli", outcome="refused", reason="another_run_in_progress")
                raise SystemExit(2)
        return self

    def __exit__(self, *exc):
        if self.enabled:
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass


def repo_for(args, config):
    code = CONFIGS[config]["code"]
    repo = getattr(args, CODE_REPO_ARG[code], None)
    if not repo:
        log_event("chat_eval.cli", outcome="refused", reason="missing_%s_repo" % code, config=config)
        raise SystemExit(2)
    return repo


def run_workers(args, run_dir, ledger, configs, only=""):
    existing = {a["config"] for a in read_jsonl(run_dir / "answers.jsonl")}
    if existing & set(configs) and not getattr(args, "allow_existing", False):
        # A second run would pay for new answers and leave the old verdicts attached to them.
        log_event("chat_eval.cli", outcome="refused", reason="answers_already_exist")
        return 2
    run_dir.mkdir(parents=True, exist_ok=True)
    claude_measure = getattr(args, "claude", False) or CONFIGS[configs[0]]["code"] in ("v2", "v3")
    for config in configs:
        repo = repo_for(args, config)
        provider = CONFIGS[config]["provider"]
        paid_cap = getattr(args, "anthropic_budget_usd", ANTHROPIC_EVAL_LIMIT_USD) if provider == "anthropic" and args.real else args.budget_usd
        default_runs = 1 if claude_measure else DEFAULT_RUNS.get(config, 1)
        command = [sys.executable, "-m", "tools.chat_eval.worker", "--repo", str(repo), "--config", config,
                   "--out-dir", str(run_dir), "--ledger", str(ledger), "--budget-usd", str(paid_cap),
                   "--runs", str(getattr(args, "runs", 0) or (1 if only else default_runs)), "--charts", args.charts]
        if only:
            command += ["--only", only]
        if args.params_func:
            command += ["--params-func", args.params_func]
        if provider == "anthropic":
            dry_url, rehearsal = args.anthropic_base_url, getattr(args, "anthropic_real_base_url", "")
        else:
            dry_url, rehearsal = args.base_url, args.real_base_url
        if args.real:
            command.append("--real")
            if rehearsal:
                command += ["--real-base-url", rehearsal]
        else:
            command += ["--base-url", dry_url]
        env = child_env(real=args.real, base_url=(rehearsal or None) if args.real else dry_url, provider=provider)
        result = subprocess.run(command, cwd=str(TOOL_ROOT), env=env, capture_output=True, text=True,
                                encoding="utf-8", errors="replace")
        for line in result.stdout.splitlines():
            if line.startswith("chat_eval."):
                print(redact(line), flush=True)
        # Child stderr (library warnings) goes to a file, redacted; it is never echoed.
        (run_dir / ("worker-%s.err" % config)).write_text(redact(result.stderr), encoding="utf-8")
        if result.returncode != 0:
            log_event("chat_eval.cli", outcome="worker_failed", config=config, exit_code=result.returncode)
            return result.returncode
    return 0


def make_metered(args, run_dir, ledger, config_label):
    from openai import OpenAI
    options = {"timeout": 180, "max_retries": 0,
               "base_url": client_base_url(real=args.real, dry_url=args.base_url, real_base_url=args.real_base_url)}
    if not args.real:
        refuse_non_loopback()
    return MeteredClient(OpenAI(**options), Budget(args.budget_usd, ledger), JsonlWriter(run_dir / "calls.jsonl"),
                         config=config_label)


def make_metered_claude(args, run_dir, ledger, config_label):
    """The Claude judge's counter: the official address in a paid run whatever the environment says, max_retries=0."""
    import anthropic
    url = client_base_url(real=args.real, dry_url=getattr(args, "anthropic_base_url", STAND_ANTHROPIC_URL),
                          real_base_url=getattr(args, "anthropic_real_base_url", ""), provider="anthropic")
    if not args.real:
        refuse_non_loopback()
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"], base_url=url, timeout=180, max_retries=0)
    cap = getattr(args, "anthropic_budget_usd", ANTHROPIC_EVAL_LIMIT_USD) if args.real else args.budget_usd
    return MeteredAnthropic(client, Budget(cap, ledger, provider="anthropic"), JsonlWriter(run_dir / "calls.jsonl"), config=config_label)


def make_judge_counter(args, run_dir, ledger, name):
    return (make_metered_claude if name == "claude" else make_metered)(args, run_dir, ledger, "judge")


def judge_workers(args, name):
    given = getattr(args, "judge_workers", None)
    return given or (2 if name == "claude" else 4)          # a new Anthropic workspace has low rate limits: two threads


def judgment_keys(path):
    return {(j.get("judge", "openai"), j["left"], j["right"], j["q"], j["run"], j["order"]) for j in read_jsonl(path)
            if j["outcome"] != "budget_stop"}


def total_spent(ledger):
    return round(sum(spent_by_provider(ledger).values()), 6)


# ------------------------------------------------------------------------------------------- earlier measurement
def judge_stage(args, run_dir, ledger, pairs):
    answers = read_jsonl(run_dir / "answers.jsonl")
    facts_path = run_dir / "facts.json"
    if not answers or not facts_path.is_file():
        log_event("chat_eval.cli", outcome="refused", reason="no_answers_to_judge")
        return 2
    done = {(j["left"], j["right"], j["q"], j["run"], j["order"]) for j in read_jsonl(run_dir / "judgments.jsonl")
            if j["outcome"] != "budget_stop" and j.get("judge", "openai") == "openai"}
    present = {a["config"] for a in answers}
    wanted = [(a, b) for a, b in pairs if a in present and b in present]
    metered = make_metered(args, run_dir, ledger, "judge")
    records = run_judge(metered, json.loads(json.dumps(answers)), json.loads(facts_path.read_text(encoding="utf-8")), wanted,
                        JsonlWriter(run_dir / "judgments.jsonl"), workers=args.judge_workers or 4, done=done)
    log_event("chat_eval.judge", outcome="stopped" if metered.budget_stopped else "done", judgments=len(records),
              spent_usd=round(Budget(args.budget_usd, ledger).spent(), 6))
    return 3 if metered.budget_stopped else 0


def default_pairs(configs):
    pairs = [("C0", "C1")]
    for a, b in (("C1", "C2"), ("C2", "C3")):
        if a in configs and b in configs:
            pairs.append((a, b))
    return pairs


def report_stage(args, run_dir, ledger):
    charts = json.loads(Path(args.charts).read_text(encoding="utf-8"))
    present = {a["config"] for a in read_jsonl(run_dir / "answers.jsonl")}
    metrics = build_report(run_dir, args.seed or run_dir.name, ledger, args.budget_usd, charts, default_pairs(present))
    log_event("chat_eval.report", outcome="written", spent_usd=metrics["spent_usd"], pages=len(metrics["pages"]),
              projected_usd=metrics["projection"]["likely_usd"])
    return 0


# ------------------------------------------------------------------------------------------------- CHAT-CLAUDE
def load_answers_and_facts(run_dir):
    answers = read_jsonl(run_dir / "answers.jsonl")
    facts_path = run_dir / "facts.json"
    if not answers or not facts_path.is_file():
        log_event("chat_eval.cli", outcome="refused", reason="no_answers_to_judge")
        return None, None
    return json.loads(json.dumps(answers)), json.loads(facts_path.read_text(encoding="utf-8"))


def judge_pairs(args, run_dir, ledger, name, pairs, answers, facts, only_q=None):
    """One judge on some pairs of configurations; a pair is judged only when both configurations have answers. Returns (calls, stopped)."""
    present = {a["config"] for a in answers}
    wanted = [(a, b) for a, b in pairs if a in present and b in present]
    if not wanted:
        return 0, False
    counter = make_judge_counter(args, run_dir, ledger, name)
    records = run_judge(counter, answers, facts, wanted, JsonlWriter(run_dir / "judgments.jsonl"), workers=judge_workers(args, name),
                        done=judgment_keys(run_dir / "judgments.jsonl"), judge=name, model=args.claude_judge_model, only_q=only_q)
    return len(records), counter.budget_stopped


def stage_controls(args, run_dir, ledger):
    """J0: the calibration of every chosen judge on the six control pairs and the two attribution controls (no model answers needed)."""
    run_dir.mkdir(parents=True, exist_ok=True)
    charts = json.loads(Path(args.charts).read_text(encoding="utf-8"))
    facts = {"natal": {k: ChartFacts(v).for_judge() for k, v in charts.items()}, "pairs": {}}
    pairs_path, attribution_path, result_path = (run_dir / "judgments-controls.jsonl", run_dir / "attribution-controls.jsonl",
                                                 run_dir / "controls.json")
    results = json.loads(result_path.read_text(encoding="utf-8")) if result_path.is_file() else {}
    stopped = False
    for name in effective_judges(args):
        counter = make_judge_counter(args, run_dir, ledger, name)
        model = args.claude_judge_model
        run_judge(counter, controls.control_records(), facts, [(controls.BAD, controls.GOOD)], JsonlWriter(pairs_path),
                  workers=judge_workers(args, name), done=judgment_keys(pairs_path), judge=name, model=model)
        seen = {(r["judge"], r["control"], r["order"]) for r in read_jsonl(attribution_path) if r["outcome"] != "budget_stop"}
        writer = JsonlWriter(attribution_path)
        for job in controls.control_attribution_jobs(facts):
            if (name, job["control"], job["order"]) not in seen:
                writer.write(attribution.run_job(counter, job, judge=name, model=model))
        stopped = stopped or counter.budget_stopped
        results[name] = controls.evaluate_j0([j for j in read_jsonl(pairs_path) if j.get("judge", "openai") == name],
                                             [r for r in read_jsonl(attribution_path) if r["judge"] == name])
    result_path.write_text(json.dumps(results, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    failed = sorted(name for name, result in results.items() if not result["passed"])
    outcome = "stopped" if stopped else ("j0_failed" if failed else "j0_passed")
    log_event("chat_eval.judge", stage="controls", outcome=outcome, judges=len(results), failed=len(failed), spent_usd=total_spent(ledger))
    return 3 if stopped else (EXIT_J0_FAILED if failed else 0)


def stage_effort(args, run_dir, ledger):
    """J3: the costly effort against the cheap one on the ten questions of S10 (Claude judge unless --judges says otherwise)."""
    answers, facts = load_answers_and_facts(run_dir)
    if answers is None:
        return 2
    stopped, calls = False, 0
    judges = tuple(x.strip() for x in args.judges.split(",") if x.strip()) if getattr(args, "judges", None) else ("claude",)
    for name in judges:
        done, halt = judge_pairs(args, run_dir, ledger, name, (("G3", "G3x"), ("H3", "H3x")), answers, facts, only_q=set(Q.S10_IDS))
        calls, stopped = calls + done, stopped or halt
    log_event("chat_eval.judge", stage="effort", outcome="stopped" if stopped else "done", judgments=calls, spent_usd=total_spent(ledger))
    return 3 if stopped else 0


def stage_main(args, run_dir, ledger, charts):
    """J1 (both judges), J2 (OpenAI judge) and J4 (the judge of the other family) on the finalists chosen by J3."""
    answers, facts = load_answers_and_facts(run_dir)
    if answers is None:
        return 2
    run = load_run(run_dir)
    g, h = choose_finals(run, code_metrics(run["answers"], charts), args.g_final, args.h_final)
    judges = effective_judges(args)
    stopped, calls = False, 0
    for name in judges:
        done, halt = judge_pairs(args, run_dir, ledger, name, ((g, h),), answers, facts)
        calls, stopped = calls + done, stopped or halt
    if "openai" in judges and not stopped:
        done, halt = judge_pairs(args, run_dir, ledger, "openai", (("G2", "G3"), ("H2", "H3")), answers, facts)
        calls, stopped = calls + done, stopped or halt
    attribution_path = run_dir / "attribution.jsonl"
    seen = {(r["config"], r["judge"], r["pair"], r["order"]) for r in read_jsonl(attribution_path) if r["outcome"] != "budget_stop"}
    counters, writer = {}, JsonlWriter(attribution_path)
    for config in dict.fromkeys(["C0", "G2", g, "H2", h]):
        name = attribution.attribution_judge_for(config)
        if name not in judges or stopped:
            continue
        for job in attribution.attribution_jobs(answers, facts, config):
            if (config, name, job["pair"], job["order"]) in seen:
                continue
            counters.setdefault(name, make_judge_counter(args, run_dir, ledger, name))
            writer.write(attribution.run_job(counters[name], job, judge=name, model=args.claude_judge_model))
            calls += 1
            if counters[name].budget_stopped:
                stopped = True
                break
    log_event("chat_eval.judge", stage="main", outcome="stopped" if stopped else "done", judgments=calls, g=g, h=h,
              spent_usd=total_spent(ledger))
    return 3 if stopped else 0


def report_claude_stage(args, run_dir, ledger):
    charts = json.loads(Path(args.charts).read_text(encoding="utf-8"))
    limits = {"openai": args.budget_usd, "anthropic": getattr(args, "anthropic_budget_usd", ANTHROPIC_EVAL_LIMIT_USD)}
    metrics = build_claude_report(run_dir, args.seed or run_dir.name, ledger, limits, charts, g_final=args.g_final, h_final=args.h_final,
                                  owner_line=args.owner_line)
    log_event("chat_eval.report", outcome="written", kind="claude", pages=len(metrics["pages"]), spent_usd=sum(metrics["spend_by_provider"].values()),
              choice=metrics["recommendation"]["choice"], basis=metrics["recommendation"]["basis"])
    return 0


def cycle_claude(args, run_dir, ledger):
    """The whole dry chain: run, J0 (continues after a failure: a fake judge cannot pass it), J3, J1/J2/J4, report."""
    configs = config_list(args)
    code = run_workers(args, run_dir, ledger, configs, only=args.only)
    if code:
        return code
    charts = json.loads(Path(args.charts).read_text(encoding="utf-8"))
    for step in (lambda: stage_controls(args, run_dir, ledger), lambda: stage_effort(args, run_dir, ledger),
                 lambda: stage_main(args, run_dir, ledger, charts)):
        code = step()
        if code == 3 or (code not in (0, EXIT_J0_FAILED)):
            report_claude_stage(args, run_dir, ledger)
            return code
    report_claude_stage(args, run_dir, ledger)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m tools.chat_eval", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("probe", "run", "judge", "report", "cycle"):
        command = sub.add_parser(name)
        add_common(command)
        if name in ("probe", "run", "cycle"):
            command.add_argument("--configs", default="" if name == "probe" else None,
                                 help="comma separated; run/cycle default C0,C1 (with --claude: %s), probe default %s" % (
                                     V2_CONFIGS, ",".join(PROBE_CONFIGS)))
        if name in ("run", "cycle"):
            command.add_argument("--only", default="", help="comma separated question ids (a smoke run)")
            command.add_argument("--runs", type=int, default=0, help="runs per configuration (earlier measurement: 2 for C0/C1; CHAT-CLAUDE: 1)")
            command.add_argument("--allow-existing", action="store_true",
                                 help="run again on a folder that already has answers of these configurations (pays again)")
        if name in ("judge", "cycle"):
            command.add_argument("--judge-workers", type=int, default=None)
        if name == "judge":
            command.add_argument("--stage", choices=STAGES, default=None, help="CHAT-CLAUDE stage; without it the earlier OpenAI-only judging")
        if name in ("report", "cycle"):
            command.add_argument("--claude", action="store_true", help="the CHAT-CLAUDE chain / report")
    args = parser.parse_args(argv)
    if args.command == "cycle" and getattr(args, "claude", False):
        if args.real:
            refuse("cycle_is_dry_only")
        args.configs = args.configs or V2_CONFIGS
    if args.real and not args.secrets_dir:
        args.secrets_dir = str(default_secrets_dir())        # the only place that names the real key folder
    run_dir, ledger = prepare(args)
    with RunLock(ledger, args.real and args.command != "report"):
        if args.command == "probe":
            configs = config_list(args) or list(PROBE_CONFIGS)
            return run_workers(args, run_dir / "probe", ledger, configs, only="N01")
        if args.command == "judge" and args.stage:
            if args.stage == "controls":
                return stage_controls(args, run_dir, ledger)
            if args.stage == "effort":
                return stage_effort(args, run_dir, ledger)
            return stage_main(args, run_dir, ledger, json.loads(Path(args.charts).read_text(encoding="utf-8")))
        if args.command == "report" and args.claude:
            return report_claude_stage(args, run_dir, ledger)
        if args.command == "cycle" and args.claude:
            return cycle_claude(args, run_dir, ledger)
        configs = config_list(args) or ["C0", "C1"]
        if any(c not in CONFIGS or c.startswith("P") for c in configs):
            log_event("chat_eval.cli", outcome="refused", reason="unknown_config")
            return 2
        code = 0
        if args.command in ("run", "cycle"):
            code = run_workers(args, run_dir, ledger, configs, only=args.only)
        if code == 0 and args.command in ("judge", "cycle"):
            code = judge_stage(args, run_dir, ledger, default_pairs(configs if args.command == "cycle" else
                                                                    {a["config"] for a in read_jsonl(run_dir / "answers.jsonl")}))
        if args.command in ("report", "cycle") or code == 3:
            report_stage(args, run_dir, ledger)
        return code


if __name__ == "__main__":
    sys.exit(main())
