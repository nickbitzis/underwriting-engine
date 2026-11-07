"""Command line: serve, decide, diff-rules, fair-lending, make-samples."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from pathlib import Path

from .engine import decide
from .impact import diff_rules, portfolio_fair_lending, render_markdown
from .models import loan_file_from_dict, loan_file_to_dict, to_engine_input
from .rules import DEFAULT_RULES_DIR, load_all, load_rules, select_version
from .synth import generate_portfolio

APP_ROOT = Path(__file__).resolve().parents[2]
CURRENT_RULES = DEFAULT_RULES_DIR / "uw-rules-2026.11.0.yaml"
CANDIDATE_RULES = DEFAULT_RULES_DIR / "candidate" / "uw-rules-2027.01.0.yaml"


def _portfolio(args):
    files = generate_portfolio(args.n, seed=args.seed)
    if getattr(args, "include_samples", True):
        for p in sorted((APP_ROOT / "data" / "samples").glob("*.json")):
            files.append(loan_file_from_dict(json.loads(p.read_text())))
    return files


def cmd_serve(args):
    import uvicorn

    uvicorn.run(
        "uwengine.app:create_app",
        factory=True,
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", args.port)),
        access_log=False,
    )


def cmd_decide(args):
    lf = loan_file_from_dict(json.loads(Path(args.file).read_text()))
    rules = load_rules(args.rules) if args.rules else select_version(load_all(), lf.application_date)
    d = decide(to_engine_input(lf), rules).to_dict()
    if args.full:
        print(json.dumps(d, indent=2, default=str))
        return
    print(
        f"{d['application_id']}  {d['outcome'].upper()}  rules {d['rules_version']}  "
        f"rate {d['pricing']['note_rate']:.3f}%"
    )
    for k, v in d["metrics"].items():
        print(f"  {k:26s} {v}")
    for i, r in enumerate(d["adverse_action_reasons"], 1):
        print(f"  reason {i} ({r['code']}): {r['text']}")
    for r in d["refer_reasons"]:
        print(f"  refer: {r}")
    for c in d["conditions"]:
        print(f"  condition: {c['text']}")


def cmd_diff(args):
    report = diff_rules(_portfolio(args), load_rules(args.current), load_rules(args.candidate))
    md = render_markdown(report)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(md)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if args.step_summary and summary:
        with open(summary, "a") as f:
            f.write(md)
    if args.fail_over is not None and report["changed"] / max(1, report["applications"]) > args.fail_over:
        print(
            f"impact {report['changed']}/{report['applications']} is above --fail-over {args.fail_over}",
            file=sys.stderr,
        )
        sys.exit(1)


def cmd_fair(args):
    files = generate_portfolio(args.n, seed=args.seed)
    report = portfolio_fair_lending(files, load_rules(args.rules))
    if args.json:
        print(json.dumps(report, indent=2))
        return
    print(f"Fair lending monitoring, {report['applications']} applications, rules {report['rules_version']}")
    print(f"Reference group: {report['reference_group']}")
    print(
        f"{'group':10s} {'apps':>5s} {'decided':>8s} {'approved':>9s} {'rate':>7s} {'AIR':>6s} "
        f"{'p-value':>8s} {'mean rate':>10s} {'raw bps':>8s} {'adj bps':>8s}"
    )
    for g, v in report["groups"].items():
        print(
            f"{g:10s} {v['applications']:5d} {v['decided']:8d} {v['approved']:9d} {v['approval_rate']:7.1%} "
            f"{v['air']:6.2f} {v['approval_p_value'] if v['approval_p_value'] is not None else '-':>8} "
            f"{v['mean_note_rate']:10.3f} {v['raw_rate_diff_bps']:8.1f} "
            f"{v['adjusted_rate_diff_bps'] if v['adjusted_rate_diff_bps'] is not None else 'n/a':>8}"
        )
    print("Alerts:" if report["alerts"] else "Alerts: none")
    for a in report["alerts"]:
        print(f"  {a['message']}")
    print(report["note"])


def cmd_samples(args):
    """Pick one file per interesting outcome from the seeded portfolio and save it under data/samples."""
    rules = load_rules(CURRENT_RULES)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    wanted = [
        ("bank_statement", "approve", "Bank statement, clean approve"),
        ("bank_statement", "approve_with_conditions", "Bank statement, large deposit needs LOE"),
        ("bank_statement", "refer", "Bank statement, manual review"),
        ("bank_statement", "decline", "Bank statement, decline"),
        ("dscr", "approve", "DSCR investor, approve"),
        ("dscr", "refer", "DSCR investor, coverage under 1.00"),
        ("dscr", "decline", "DSCR investor, decline"),
    ]
    taken = Counter()
    for lf in generate_portfolio(400, seed=args.seed):
        if lf.application_date < str(rules.effective_date):
            continue
        d = decide(to_engine_input(lf), rules, decision_id="s")
        for prog, outcome, label in wanted:
            if taken[(prog, outcome)] or d.program != prog or d.outcome != outcome or d.rules_version != rules.version:
                continue
            if outcome == "approve_with_conditions" and not (d.income_worksheet or {}).get("large_deposits"):
                continue
            if outcome == "approve" and prog == "bank_statement" and lf.statements.months != 24:
                continue
            if outcome == "decline" and len(d.adverse_action_reasons) < 2:
                continue
            taken[(prog, outcome)] += 1
            data = {"label": label, **loan_file_to_dict(lf)}
            (out / f"{lf.id}.json").write_text(json.dumps(data, indent=1))
            print(f"{lf.id}  {label}")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="uwengine")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve")
    s.add_argument("--port", default="8782")
    s.set_defaults(fn=cmd_serve)

    s = sub.add_parser("decide", help="decide one loan file JSON")
    s.add_argument("file")
    s.add_argument("--rules", help="rules file; default is the version in effect on the application date")
    s.add_argument("--full", action="store_true", help="print the full audit record")
    s.set_defaults(fn=cmd_decide)

    s = sub.add_parser("diff-rules", help="which applications change decision under a candidate rules file")
    s.add_argument("--current", default=str(CURRENT_RULES))
    s.add_argument("--candidate", default=str(CANDIDATE_RULES))
    s.add_argument("--n", type=int, default=300)
    s.add_argument("--seed", type=int, default=7)
    s.add_argument("--json", action="store_true")
    s.add_argument("--step-summary", action="store_true", help="append markdown to $GITHUB_STEP_SUMMARY")
    s.add_argument("--fail-over", type=float, default=None, help="exit 1 if the changed share is above this")
    s.set_defaults(fn=cmd_diff)

    s = sub.add_parser("fair-lending", help="approval rate ratios and pricing differences by monitoring group")
    s.add_argument("--rules", default=str(CURRENT_RULES))
    s.add_argument("--n", type=int, default=300)
    s.add_argument("--seed", type=int, default=7)
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_fair)

    s = sub.add_parser("make-samples", help="regenerate data/samples from the seeded portfolio")
    s.add_argument("--out", default=str(APP_ROOT / "data" / "samples"))
    s.add_argument("--seed", type=int, default=7)
    s.set_defaults(fn=cmd_samples)

    args = ap.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
