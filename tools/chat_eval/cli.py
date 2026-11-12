"""Command line: probe, run, judge, report, cycle. Dry by default; a paid run needs --real and the owner's key file.

  python -m tools.chat_eval cycle --base-repo <base checkout> --candidate-repo <candidate checkout>          (dry)
  python -m tools.chat_eval cycle --real --base-repo ... --candidate-repo ... --configs C0,C1,C2,C3
Output lines are finite events (labels and numbers); texts never reach the console or the logs.
"""
import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from .budget import Budget
from .common import (BUDGET_DEFAULT_USD, CONFIGS, STAND_OPENAI_URL, STUB_KEY, TOOL_DIR, KeyError_, child_env,
                     client_base_url, default_key_file, default_tasks_dir, is_loopback_url, load_key, log_event,
                     paid_state_dir, redact, refuse_non_loopback, valid_budget)
from .judge import run_judge
from .metered import JsonlWriter, MeteredClient
from .report import build_report, read_jsonl

TOOL_ROOT = TOOL_DIR.parent.parent
DEFAULT_RUNS = {"C0": 2, "C1": 2, "C2": 1, "C3": 1, "C4": 1, "P0": 1, "P1": 1, "P2": 1}


def add_common(parser):
    parser.add_argument("--real", action="store_true", help="paid OpenAI with the key from the owner's file (default: dry)")
    parser.add_argument("--base-repo", help="checkout with the BASE commit (configuration C0, probe P0)")
    parser.add_argument("--candidate-repo", help="checkout with the candidate branch (C1-C4, probes P1/P2)")
    parser.add_argument("--run-dir", help="default: <tasks>/eval-dry (dry) or <tasks>/eval/<run id> (real)")
    parser.add_argument("--budget-usd", type=float, default=BUDGET_DEFAULT_USD)
    parser.add_argument("--key-file", default=None)
    parser.add_argument("--base-url", default=STAND_OPENAI_URL, help="dry mode: the loopback OpenAI stand-in")
    parser.add_argument("--real-base-url", default="",
                        help="rehearsal of the paid code path against a LOOPBACK server (tests); never an external address")
    parser.add_argument("--ledger-dir", default="",
                        help="TESTS ONLY, and only with --real --real-base-url on loopback: another folder for the paid ledger")
    parser.add_argument("--charts", default=str(TOOL_DIR / "charts.json"))
    parser.add_argument("--seed", default=None, help="seed of the blind shuffle (default: the run directory name)")
    parser.add_argument("--params-func", default="", help="module:attr of the candidate's parameter source, if not auto-detected")


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


def prepare(args):
    """Start conditions for any command that may call a provider. Returns (run_dir, ledger) or exits with a reason."""
    if not valid_budget(args.budget_usd):
        log_event("chat_eval.cli", outcome="refused", reason="budget_out_of_range")
        raise SystemExit(2)
    if args.ledger_dir and not (args.real and args.real_base_url):
        log_event("chat_eval.cli", outcome="refused", reason="ledger_dir_only_for_rehearsal")
        raise SystemExit(2)
    if args.real_base_url and not is_loopback_url(args.real_base_url):
        log_event("chat_eval.cli", outcome="refused", reason="real_base_url_must_be_loopback")
        raise SystemExit(2)
    if args.real:
        os.environ.pop("OPENAI_BASE_URL", None)          # an inherited address must never receive the key
        try:
            load_key(args.key_file or default_key_file())
        except KeyError_ as error:
            log_event("chat_eval.cli", outcome="refused", reason=str(error))
            raise SystemExit(2)
    else:
        if not is_loopback_url(args.base_url):
            log_event("chat_eval.cli", outcome="refused", reason="dry_needs_loopback_url")
            raise SystemExit(2)
        os.environ["OPENAI_API_KEY"] = STUB_KEY
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
    repo = args.base_repo if code == "base" else args.candidate_repo
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
    for config in configs:
        repo = repo_for(args, config)
        command = [sys.executable, "-m", "tools.chat_eval.worker", "--repo", str(repo), "--config", config,
                   "--out-dir", str(run_dir), "--ledger", str(ledger), "--budget-usd", str(args.budget_usd),
                   "--runs", str(getattr(args, "runs", 0) or (1 if only else DEFAULT_RUNS[config])), "--charts", args.charts]
        if only:
            command += ["--only", only]
        if args.params_func:
            command += ["--params-func", args.params_func]
        if args.real:
            command.append("--real")
            if args.real_base_url:
                command += ["--real-base-url", args.real_base_url]
        else:
            command += ["--base-url", args.base_url]
        env = child_env(real=args.real, base_url=None if args.real else args.base_url)
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


def judge_stage(args, run_dir, ledger, pairs):
    answers = read_jsonl(run_dir / "answers.jsonl")
    facts_path = run_dir / "facts.json"
    if not answers or not facts_path.is_file():
        log_event("chat_eval.cli", outcome="refused", reason="no_answers_to_judge")
        return 2
    done = {(j["left"], j["right"], j["q"], j["run"], j["order"]) for j in read_jsonl(run_dir / "judgments.jsonl")
            if j["outcome"] != "budget_stop"}
    present = {a["config"] for a in answers}
    wanted = [(a, b) for a, b in pairs if a in present and b in present]
    metered = make_metered(args, run_dir, ledger, "judge")
    records = run_judge(metered, json.loads(json.dumps(answers)), json.loads(facts_path.read_text(encoding="utf-8")), wanted,
                        JsonlWriter(run_dir / "judgments.jsonl"), workers=args.judge_workers, done=done)
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


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m tools.chat_eval", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("probe", "run", "judge", "report", "cycle"):
        command = sub.add_parser(name)
        add_common(command)
        if name in ("run", "cycle"):
            command.add_argument("--configs", default="C0,C1", help="comma separated: C0,C1,C2,C3,C4")
            command.add_argument("--only", default="", help="comma separated question ids (a smoke run)")
            command.add_argument("--runs", type=int, default=0, help="runs per configuration (default 2 for C0/C1, else 1)")
            command.add_argument("--allow-existing", action="store_true",
                                 help="run again on a folder that already has answers of these configurations (pays again)")
        if name in ("judge", "cycle"):
            command.add_argument("--judge-workers", type=int, default=4)
    args = parser.parse_args(argv)
    run_dir, ledger = prepare(args)
    with RunLock(ledger, args.real and args.command != "report"):
        if args.command == "probe":
            return run_workers(args, run_dir / "probe", ledger, ["P0", "P1", "P2"], only="N01")
        configs = [c.strip() for c in getattr(args, "configs", "C0,C1").split(",") if c.strip()]
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
