"""Impact analysis: which existing applications would change decision under a new rules version."""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime

from .engine import OUTCOMES, decide
from .fairlending import fair_lending_report
from .models import LoanFile, to_engine_input
from .rules import RuleSet

FIXED_TIME = datetime(2026, 11, 7, tzinfo=UTC)


def diff_rules(files: list[LoanFile], current: RuleSet, candidate: RuleSet) -> dict:
    changed = []
    transitions: Counter = Counter()
    rate_moves = []
    for lf in files:
        inp = to_engine_input(lf)
        a = decide(inp, current, decision_id="current", now=FIXED_TIME)
        b = decide(inp, candidate, decision_id="candidate", now=FIXED_TIME)
        transitions[(a.outcome, b.outcome)] += 1
        if a.outcome in ("approve", "approve_with_conditions") and b.outcome in ("approve", "approve_with_conditions"):
            rate_moves.append(b.pricing["note_rate"] - a.pricing["note_rate"])
        if a.outcome != b.outcome:
            new_reasons = [
                r.code for r in b.adverse_action_reasons if r.code not in {x.code for x in a.adverse_action_reasons}
            ]
            changed.append(
                {
                    "application_id": lf.id,
                    "program": lf.loan.program,
                    "from": a.outcome,
                    "to": b.outcome,
                    "new_reasons": new_reasons,
                    "new_refer_reasons": [r for r in b.refer_reasons if r not in a.refer_reasons],
                    "rate_from": a.pricing["note_rate"],
                    "rate_to": b.pricing["note_rate"],
                }
            )
    before = Counter(t[0] for t in transitions.elements())
    after = Counter(t[1] for t in transitions.elements())
    repriced = [m for m in rate_moves if abs(m) > 1e-9]
    return {
        "current": {
            "version": current.version,
            "effective_date": str(current.effective_date),
            "sha256": current.sha256,
        },
        "candidate": {
            "version": candidate.version,
            "effective_date": str(candidate.effective_date),
            "sha256": candidate.sha256,
        },
        "applications": len(files),
        "changed": len(changed),
        "outcomes_before": {o: before.get(o, 0) for o in OUTCOMES},
        "outcomes_after": {o: after.get(o, 0) for o in OUTCOMES},
        "transitions": [{"from": f, "to": t, "count": n} for (f, t), n in sorted(transitions.items()) if f != t],
        "repriced_approvals": len(repriced),
        "mean_rate_change_bps": round(sum(repriced) / len(repriced) * 100, 1) if repriced else 0.0,
        "changes": changed,
    }


def render_markdown(report: dict, limit: int = 25) -> str:
    c, k = report["current"], report["candidate"]
    lines = [
        f"## Rules impact: {c['version']} to {k['version']}",
        "",
        f"{report['changed']} of {report['applications']} applications change decision. "
        f"{report['repriced_approvals']} approvals that stay approved are repriced, "
        f"mean change {report['mean_rate_change_bps']:+.1f} bps.",
        "",
        "| Outcome | Current | Candidate | Change |",
        "|---|---:|---:|---:|",
    ]
    for o in OUTCOMES:
        a, b = report["outcomes_before"][o], report["outcomes_after"][o]
        lines.append(f"| {o} | {a} | {b} | {b - a:+d} |")
    if report["transitions"]:
        lines += ["", "| From | To | Applications |", "|---|---|---:|"]
        lines += [f"| {t['from']} | {t['to']} | {t['count']} |" for t in report["transitions"]]
    if report["changes"]:
        lines += [
            "",
            f"First {min(limit, len(report['changes']))} changed applications:",
            "",
            "| Application | Program | From | To | New reasons |",
            "|---|---|---|---|---|",
        ]
        for ch in report["changes"][:limit]:
            why = ", ".join(ch["new_reasons"]) or "; ".join(ch["new_refer_reasons"]) or "conditions changed"
            lines.append(f"| {ch['application_id']} | {ch['program']} | {ch['from']} | {ch['to']} | {why} |")
    return "\n".join(lines) + "\n"


def portfolio_rows(files: list[LoanFile], rules: RuleSet) -> list[dict]:
    """Decide every file, then join the monitoring attribute back on by application id."""
    groups = {lf.id: lf.monitoring.protected_group for lf in files if lf.monitoring}
    rows = []
    for lf in files:
        d = decide(to_engine_input(lf), rules, decision_id="portfolio", now=FIXED_TIME)
        if lf.id not in groups:
            continue
        rows.append(
            {
                "application_id": lf.id,
                "group": groups[lf.id],
                "outcome": d.outcome,
                "note_rate": d.pricing["note_rate"],
                "fico": lf.borrower.credit_score,
                "ltv": d.metrics["ltv"],
                "program": d.program,
            }
        )
    return rows


def portfolio_fair_lending(files: list[LoanFile], rules: RuleSet) -> dict:
    report = fair_lending_report(portfolio_rows(files, rules))
    report["rules_version"] = rules.version
    report["applications"] = len(files)
    return report
