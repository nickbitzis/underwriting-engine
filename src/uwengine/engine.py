"""The decision engine. Deterministic: same EngineInput and same RuleSet give the same decision.

The engine takes an EngineInput, never a LoanFile, so it has no access to monitoring data.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import asdict
from datetime import UTC, datetime

from . import __version__
from .income import bank_statement_income, dscr_worksheet, housing_payment, monthly_pi
from .models import Condition, Decision, EngineInput, Reason, RuleCheck
from .pricing import PricingError, fico_row, points_ladder, price
from .rules import RuleSet

CLOSING_COST_PCT = 0.03  # estimated closing costs as a share of the loan amount

OUTCOMES = ("approve", "approve_with_conditions", "refer", "decline")


class EngineError(ValueError):
    pass


def input_hash(inp: EngineInput) -> str:
    blob = json.dumps(asdict(inp), sort_keys=True, default=str).encode()
    return hashlib.sha256(blob).hexdigest()


def value_basis(inp: EngineInput) -> float:
    p = inp.property
    if inp.loan.purpose == "purchase" and p.purchase_price:
        return min(p.appraised_value, p.purchase_price)
    return p.appraised_value


def max_ltv_for(rules: RuleSet, inp: EngineInput) -> tuple[float, list[str]]:
    prod = rules.product(inp.loan.program)
    row = fico_row(prod["max_ltv"], inp.borrower.credit_score)
    if row is None:
        return 0.0, [f"products.{inp.loan.program}.max_ltv: no row for FICO {inp.borrower.credit_score}"]
    applied = [f"products.{inp.loan.program}.max_ltv[fico_min={row['fico_min']}].{inp.loan.purpose}"]
    m = row[inp.loan.purpose]
    jumbo = prod.get("jumbo")
    if jumbo and inp.loan.amount > jumbo["threshold"]:
        m -= jumbo["ltv_reduction"]
        applied.append(f"products.{inp.loan.program}.jumbo.ltv_reduction")
    overlay = rules.data.get("state_overlays", {}).get(inp.property.state, {})
    if overlay.get("max_ltv_reduction"):
        m -= overlay["max_ltv_reduction"]
        applied.append(f"state_overlays.{inp.property.state}.max_ltv_reduction")
    return round(m, 4), applied


def _reason(rules: RuleSet, code: str, **params) -> Reason:
    spec = rules["adverse_action"]["reasons"][code]
    return Reason(code=code, priority=spec["priority"], text=spec["text"].format(**params))


def order_reasons(reasons: list[Reason], max_reasons: int) -> list[Reason]:
    """Principal reasons first, by the priority set in the rules. Ties keep insertion order."""
    return sorted(reasons, key=lambda r: r.priority)[:max_reasons]


def _price_for_ratios(rules: RuleSet, inp: EngineInput, ltv: float, dscr_guess: float | None) -> tuple[dict, bool]:
    """Price the loan. If the file is outside the grid, price at the nearest cell so the ratios
    can still be computed, and mark the price as not offered."""
    prog = inp.loan.program
    p = rules["pricing"]
    kw = dict(
        program=prog,
        purpose=inp.loan.purpose,
        property_type=inp.property.property_type,
        state=inp.property.state,
        loan_amount=inp.loan.amount,
        lock_days=inp.loan.lock_days,
        points=inp.loan.points,
        statement_months=inp.statements.months if inp.statements else None,
        dscr=dscr_guess,
    )
    try:
        return price(rules, fico=inp.borrower.credit_score, ltv=ltv, **kw), True
    except PricingError:
        fico = max(inp.borrower.credit_score, min(r["fico_min"] for r in p["llpa_grid"]))
        return price(rules, fico=fico, ltv=min(ltv, p["ltv_bands"][-1]), **kw), False


def decide(inp: EngineInput, rules: RuleSet, decision_id: str | None = None, now: datetime | None = None) -> Decision:
    if not isinstance(inp, EngineInput):
        raise EngineError("decide() takes an EngineInput; build it with to_engine_input(loan_file)")
    prog = inp.loan.program
    prod = rules.product(prog)
    b, prop, loan = inp.borrower, inp.property, inp.loan
    inc_rules = rules["income"]
    checks: list[RuleCheck] = []
    declines: list[Reason] = []
    refers: list[str] = []
    conditions: list[Condition] = []

    def check(rule, passed, actual, limit, outcome_if_failed):
        checks.append(RuleCheck(rule, bool(passed), actual, limit, outcome_if_failed))
        return passed

    # Property and credit eligibility
    if not check(
        "ineligible_states",
        prop.state not in rules.data.get("ineligible_states", []),
        prop.state,
        rules.data.get("ineligible_states", []),
        "decline",
    ):
        declines.append(_reason(rules, "STATE_INELIGIBLE", state=prop.state))
    if not check(
        f"products.{prog}.min_fico", b.credit_score >= prod["min_fico"], b.credit_score, prod["min_fico"], "decline"
    ):
        declines.append(_reason(rules, "CREDIT_SCORE", fico=b.credit_score, min_fico=prod["min_fico"]))
    lim = prod["loan_amount"]
    if not check(
        f"products.{prog}.loan_amount",
        lim["min"] <= loan.amount <= lim["max"],
        loan.amount,
        [lim["min"], lim["max"]],
        "decline",
    ):
        declines.append(
            _reason(rules, "LOAN_AMOUNT", loan_amount=loan.amount, min_amount=lim["min"], max_amount=lim["max"])
        )

    basis = value_basis(inp)
    ltv = loan.amount / basis
    max_ltv, ltv_rules = max_ltv_for(rules, inp)
    has_grid_row = fico_row(prod["max_ltv"], b.credit_score) is not None
    # Below the lowest credit row there is no LTV limit to state; the credit score reason covers it.
    if has_grid_row and not check(" + ".join(ltv_rules), ltv <= max_ltv + 1e-9, round(ltv, 4), max_ltv, "decline"):
        declines.append(_reason(rules, "LTV", ltv=ltv, max_ltv=max_ltv))
    jumbo = prod.get("jumbo")
    if jumbo and loan.amount > jumbo["threshold"]:
        conditions.append(
            Condition("SECOND_APPRAISAL", f"Second full appraisal for a loan amount above ${jumbo['threshold']:,.0f}.")
        )

    income_ws = None
    dscr_ws = None
    metrics: dict = {"ltv": round(ltv, 4), "max_ltv": max_ltv, "value_basis": basis}

    # A first pass at the payment so DSCR can pick its pricing band, then the real price.
    first_price, _ = _price_for_ratios(rules, inp, ltv, None)
    payment = housing_payment(loan, prop, first_price["note_rate"], prod["term_months"])
    dscr_value = None
    if prog == "dscr":
        dscr_value = prop.market_rent / payment["pitia"] if payment["pitia"] else 0.0
    pricing, offered = _price_for_ratios(rules, inp, ltv, dscr_value)
    payment = housing_payment(loan, prop, pricing["note_rate"], prod["term_months"])
    metrics["pitia"] = payment["pitia"]

    if prog == "bank_statement":
        if inp.statements is None:
            raise EngineError("bank_statement program needs statements")
        if not check(
            "income.min_years_self_employed",
            b.years_self_employed >= inc_rules["min_years_self_employed"],
            b.years_self_employed,
            inc_rules["min_years_self_employed"],
            "decline",
        ):
            declines.append(
                _reason(
                    rules,
                    "TIME_IN_BUSINESS",
                    years=b.years_self_employed,
                    min_years=inc_rules["min_years_self_employed"],
                )
            )
        income_ws = bank_statement_income(inp.statements, b, inc_rules)
        income = income_ws["qualifying_monthly_income"]
        metrics["qualifying_income"] = income
        if not check("income.qualifying_monthly_income > 0", income > 0, income, 0, "decline"):
            declines.append(_reason(rules, "INSUFFICIENT_INCOME"))
            dti = None
        else:
            dti = (b.monthly_debts + payment["pitia"]) / income
            metrics["dti"] = round(dti, 4)
            hard = prod["max_dti"] + prod["dti_refer_margin"]
            if not check(f"products.{prog}.max_dti + dti_refer_margin", dti <= hard, round(dti, 4), hard, "decline"):
                declines.append(_reason(rules, "DTI", dti=dti, max_dti=prod["max_dti"]))
            elif not check(f"products.{prog}.max_dti", dti <= prod["max_dti"], round(dti, 4), prod["max_dti"], "refer"):
                refers.append(
                    f"DTI of {dti:.1%} is above {prod['max_dti']:.0%} but within the "
                    f"{prod['dti_refer_margin']:.0%} manual review margin."
                )
        nsf = income_ws["nsf_count_12m"]
        metrics["nsf_count_12m"] = nsf
        if not check(
            "income.nsf_decline_count",
            nsf < inc_rules["nsf_decline_count"],
            nsf,
            inc_rules["nsf_decline_count"],
            "decline",
        ):
            declines.append(_reason(rules, "NSF", nsf_count=nsf))
        elif not check(
            "income.nsf_refer_count", nsf < inc_rules["nsf_refer_count"], nsf, inc_rules["nsf_refer_count"], "refer"
        ):
            refers.append(f"{nsf} NSF events in the last 12 months.")
        elif not check(
            "income.nsf_condition_count",
            nsf < inc_rules["nsf_condition_count"],
            nsf,
            inc_rules["nsf_condition_count"],
            "condition",
        ):
            dates = ", ".join(e["date"] for e in income_ws["nsf_events_12m"])
            conditions.append(Condition("LOE_NSF", f"Letter of explanation for insufficient funds events on {dates}."))
        for item in income_ws["large_deposits"]:
            conditions.append(
                Condition(
                    "LOE_LARGE_DEPOSIT",
                    f"Letter of explanation and source documents for the ${item['amount']:,.2f} deposit "
                    f"on {item['date']}.",
                    ref=item["txn_id"],
                )
            )
        check(
            "income.large_deposit_pct",
            not income_ws["large_deposits"],
            len(income_ws["large_deposits"]),
            0,
            "condition",
        )
        trend = income_ws["trend_second_half_vs_first"]
        if not check(
            "income.declining_trend_refer_pct",
            trend > -inc_rules["declining_trend_refer_pct"],
            trend,
            -inc_rules["declining_trend_refer_pct"],
            "refer",
        ):
            refers.append(
                f"Eligible deposits fell {-trend:.0%} from the first half of the statement period to the second."
            )
    else:
        dscr_ws = dscr_worksheet(prop, payment)
        d = dscr_ws["dscr"]
        metrics["dscr"] = d
        if not check(
            f"products.{prog}.dscr_refer_floor", d >= prod["dscr_refer_floor"], d, prod["dscr_refer_floor"], "decline"
        ):
            declines.append(_reason(rules, "DSCR", dscr=d, min_dscr=prod["min_dscr"]))
        elif not check(f"products.{prog}.min_dscr", d >= prod["min_dscr"], d, prod["min_dscr"], "refer"):
            refers.append(
                f"DSCR of {d:.3f} is below {prod['min_dscr']:.2f} but at or above the "
                f"{prod['dscr_refer_floor']:.2f} floor for manual review."
            )

    # Reserves after closing
    closing = loan.amount * CLOSING_COST_PCT + pricing["points_cost"]
    down = max(0.0, (prop.purchase_price or 0) - loan.amount) if loan.purpose == "purchase" else 0.0
    cash_to_close = down + closing
    reserves_months = (b.liquid_assets - cash_to_close) / payment["pitia"] if payment["pitia"] else 0.0
    overlay = rules.data.get("state_overlays", {}).get(prop.state, {})
    need = prod["min_reserves_months"] + overlay.get("reserves_months_add", 0)
    metrics.update(
        cash_to_close=round(cash_to_close, 2), reserves_months=round(reserves_months, 2), required_reserves_months=need
    )
    if not check(
        f"products.{prog}.min_reserves_months"
        + (f" + state_overlays.{prop.state}" if overlay.get("reserves_months_add") else ""),
        reserves_months >= need,
        round(reserves_months, 2),
        need,
        "decline",
    ):
        declines.append(_reason(rules, "RESERVES", reserves_months=max(reserves_months, 0), min_reserves=need))

    if declines:
        outcome = "decline"
    elif refers:
        outcome = "refer"
    elif conditions:
        outcome = "approve_with_conditions"
    else:
        outcome = "approve"

    pricing = dict(pricing)
    pricing["offered"] = offered and outcome != "decline"
    rate_no_points = pricing["note_rate"] + loan.points * rules["pricing"]["points"]["rate_per_point"]
    pricing["ladder"] = points_ladder(
        rules, rate_no_points, loan.amount, lambda r: monthly_pi(loan.amount, r, prod["term_months"])
    )
    pricing["payment"] = payment

    now = now or datetime.now(UTC)
    return Decision(
        decision_id=decision_id or "D-" + uuid.uuid4().hex[:12],
        application_id=inp.id,
        outcome=outcome,
        program=prog,
        rules_version=rules.version,
        rules_sha256=rules.sha256,
        engine_version=__version__,
        decided_at=now.isoformat(timespec="seconds"),
        as_of=inp.application_date,
        adverse_action_reasons=order_reasons(declines, rules["adverse_action"]["max_reasons"]),
        refer_reasons=refers,
        conditions=conditions,
        metrics=metrics,
        pricing=pricing,
        income_worksheet=income_ws,
        dscr_worksheet=dscr_ws,
        checks=checks,
        input_sha256=input_hash(inp),
    )


WHATIF_FIELDS = {"loan_amount", "down_payment_pct", "lock_days", "points"}


def what_if(inp: EngineInput, rules: RuleSet, variations: list[dict]) -> list[dict]:
    """Re-run the engine for each variation and return a compact row per scenario."""
    out = []
    basis = value_basis(inp)
    for v in variations:
        unknown = set(v) - WHATIF_FIELDS
        if unknown:
            raise EngineError(f"unknown what-if fields {sorted(unknown)}; allowed {sorted(WHATIF_FIELDS)}")
        changes = {}
        if "down_payment_pct" in v:
            changes["amount"] = round(basis * (1 - float(v["down_payment_pct"])) / 100) * 100
        if "loan_amount" in v:
            changes["amount"] = float(v["loan_amount"])
        if "lock_days" in v:
            changes["lock_days"] = int(v["lock_days"])
        if "points" in v:
            changes["points"] = float(v["points"])
        try:
            d = decide(inp.with_loan(**changes), rules, decision_id="whatif")
        except (PricingError, EngineError) as e:
            out.append({"inputs": v, "error": str(e)})
            continue
        out.append(
            {
                "inputs": v,
                "loan_amount": changes.get("amount", inp.loan.amount),
                "outcome": d.outcome,
                "note_rate": d.pricing["note_rate"],
                "rate_offered": d.pricing["offered"],
                "pitia": d.metrics["pitia"],
                "ltv": d.metrics["ltv"],
                "dti": d.metrics.get("dti"),
                "dscr": d.metrics.get("dscr"),
                "reserves_months": d.metrics["reserves_months"],
                "reasons": [r.code for r in d.adverse_action_reasons],
                "refer_reasons": d.refer_reasons,
                "conditions": len(d.conditions),
            }
        )
    return out
