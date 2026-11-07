"""Fair lending monitoring. Runs after decisions are made, joining them to monitoring data.

The engine never sees the protected-class attribute. This module is the only place that reads
it, and it reads it next to finished decisions. Output is a monitoring signal for compliance
review, not a legal finding of discrimination or of its absence.
"""

from __future__ import annotations

import math
from collections import defaultdict

APPROVED = {"approve", "approve_with_conditions"}
FOUR_FIFTHS = 0.80
PRICING_ALERT_BPS = 12.5


def _fico_band(fico: int) -> str:
    for lo in (780, 740, 700, 660, 620):
        if fico >= lo:
            return f"{lo}+"
    return "<620"


def _ltv_band(ltv: float) -> str:
    for hi in (0.60, 0.70, 0.75, 0.80, 0.85):
        if ltv <= hi:
            return f"<={hi:.0%}"
    return ">85%"


def two_proportion_p(x1: int, n1: int, x2: int, n2: int) -> float | None:
    """Two-sided p-value for a difference in approval rates (normal approximation)."""
    if not n1 or not n2:
        return None
    p = (x1 + x2) / (n1 + n2)
    se = math.sqrt(p * (1 - p) * (1 / n1 + 1 / n2))
    if se == 0:
        return 1.0
    z = abs(x1 / n1 - x2 / n2) / se
    return round(math.erfc(z / math.sqrt(2)), 4)


def fair_lending_report(rows: list[dict]) -> dict:
    """rows: one per decided application with keys
    group, outcome, note_rate, fico, ltv, program. Refers are pending and left out of the
    approval rate, since no credit decision has been made yet."""
    by_group: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_group[r["group"]].append(r)

    groups = {}
    for g, rs in sorted(by_group.items()):
        decided = [r for r in rs if r["outcome"] != "refer"]
        approved = [r for r in decided if r["outcome"] in APPROVED]
        rates = [r["note_rate"] for r in approved]
        groups[g] = {
            "applications": len(rs),
            "decided": len(decided),
            "approved": len(approved),
            "referred": len(rs) - len(decided),
            "approval_rate": round(len(approved) / len(decided), 4) if decided else None,
            "mean_note_rate": round(sum(rates) / len(rates), 4) if rates else None,
        }

    rated = {g: v for g, v in groups.items() if v["approval_rate"] is not None}
    alerts = []
    if not rated:
        return {"groups": groups, "reference_group": None, "alerts": alerts}
    ref = max(rated, key=lambda g: (rated[g]["approval_rate"], rated[g]["decided"]))
    ref_rate = rated[ref]["approval_rate"]
    ref_price = groups[ref]["mean_note_rate"]

    # Strata for a controlled pricing comparison: same program, credit band and LTV band.
    def strata(rs):
        out = defaultdict(list)
        for r in rs:
            if r["outcome"] in APPROVED:
                out[(r["program"], _fico_band(r["fico"]), _ltv_band(r["ltv"]))].append(r["note_rate"])
        return out

    ref_strata = strata(by_group[ref])
    for g, v in groups.items():
        v["approval_p_value"] = (
            two_proportion_p(v["approved"], v["decided"], rated[ref]["approved"], rated[ref]["decided"])
            if g != ref
            else None
        )
        v["air"] = round(v["approval_rate"] / ref_rate, 4) if v["approval_rate"] is not None and ref_rate else None
        if v["air"] is not None and v["air"] < FOUR_FIFTHS:
            alerts.append(
                {
                    "type": "approval_rate_ratio",
                    "group": g,
                    "value": v["air"],
                    "message": f"{g} approval rate is {v['air']:.2f} of {ref}'s, under the four-fifths threshold.",
                }
            )
        v["raw_rate_diff_bps"] = (
            round((v["mean_note_rate"] - ref_price) * 100, 1) if v["mean_note_rate"] is not None and ref_price else None
        )
        diffs, weight = 0.0, 0
        for key, rates in strata(by_group[g]).items():
            if key in ref_strata:
                ref_mean = sum(ref_strata[key]) / len(ref_strata[key])
                diffs += (sum(rates) / len(rates) - ref_mean) * len(rates)
                weight += len(rates)
        v["adjusted_rate_diff_bps"] = round(diffs / weight * 100, 1) if weight else None
        v["matched_loans"] = weight
        if g != ref and v["adjusted_rate_diff_bps"] is not None and v["adjusted_rate_diff_bps"] > PRICING_ALERT_BPS:
            alerts.append(
                {
                    "type": "pricing_disparity",
                    "group": g,
                    "value": v["adjusted_rate_diff_bps"],
                    "message": f"{g} pays {v['adjusted_rate_diff_bps']} bps more than {ref} within matched "
                    f"program, credit and LTV bands (alert above {PRICING_ALERT_BPS} bps).",
                }
            )
    return {
        "groups": groups,
        "reference_group": ref,
        "thresholds": {"air_min": FOUR_FIFTHS, "pricing_alert_bps": PRICING_ALERT_BPS},
        "alerts": alerts,
        "note": "Monitoring signal for compliance review. Not a legal conclusion.",
    }
