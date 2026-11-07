"""Note rate = product base rate + loan level price adjustments (LLPAs) - points buydown.

Every adjustment is a line item with the rule that produced it, so the price is explainable.
"""

from __future__ import annotations

from .rules import RuleSet


class PricingError(ValueError):
    pass


def fico_row(rows: list[dict], fico: int) -> dict | None:
    """First row (rows are in descending fico_min) whose fico_min the score meets."""
    for row in rows:
        if fico >= row["fico_min"]:
            return row
    return None


def ltv_band_index(bands: list[float], ltv: float) -> int | None:
    for i, upper in enumerate(bands):
        if ltv <= upper + 1e-9:
            return i
    return None


def _band_label(bands: list[float], i: int) -> str:
    lo = 0.0 if i == 0 else bands[i - 1]
    return f"{lo:.0%}-{bands[i]:.0%}" if i else f"<= {bands[i]:.0%}"


def price(
    rules: RuleSet,
    program: str,
    fico: int,
    ltv: float,
    purpose: str,
    property_type: str,
    state: str,
    loan_amount: float,
    lock_days: int,
    points: float,
    statement_months: int | None = None,
    dscr: float | None = None,
) -> dict:
    p = rules["pricing"]
    base = rules.product(program)["base_rate"]
    items = [{"rule": f"products.{program}.base_rate", "label": "Base rate", "adj": base}]

    row = fico_row(p["llpa_grid"], fico)
    idx = ltv_band_index(p["ltv_bands"], ltv)
    if row is None or idx is None:
        raise PricingError(f"no LLPA cell for FICO {fico} and LTV {ltv:.3f}")
    items.append(
        {
            "rule": f"pricing.llpa_grid[fico_min={row['fico_min']}][{_band_label(p['ltv_bands'], idx)}]",
            "label": f"Credit score {fico} at LTV {ltv:.1%}",
            "adj": row["adj"][idx],
        }
    )
    items.append({"rule": f"pricing.purpose.{purpose}", "label": f"Purpose: {purpose}", "adj": p["purpose"][purpose]})
    items.append(
        {
            "rule": f"pricing.property_type.{property_type}",
            "label": f"Property: {property_type}",
            "adj": p["property_type"][property_type],
        }
    )
    if program == "bank_statement" and statement_months is not None:
        items.append(
            {
                "rule": f"pricing.statement_months.{statement_months}",
                "label": f"{statement_months} months of statements",
                "adj": p["statement_months"][str(statement_months)],
            }
        )
    if program == "dscr" and dscr is not None:
        for band in p["dscr_bands"]:
            if dscr >= band["dscr_min"]:
                items.append(
                    {
                        "rule": f"pricing.dscr_bands[dscr_min={band['dscr_min']}]",
                        "label": f"DSCR {dscr:.2f}",
                        "adj": band["adj"],
                    }
                )
                break
    for band in p["loan_amount_bands"]:
        if loan_amount <= band["max"]:
            items.append(
                {
                    "rule": f"pricing.loan_amount_bands[max={band['max']}]",
                    "label": f"Loan amount ${loan_amount:,.0f}",
                    "adj": band["adj"],
                }
            )
            break
    lock_key = str(lock_days)
    if lock_key not in p["lock_days"]:
        raise PricingError(f"lock period {lock_days} days not offered, choose from {sorted(p['lock_days'])}")
    items.append(
        {"rule": f"pricing.lock_days.{lock_key}", "label": f"{lock_days}-day lock", "adj": p["lock_days"][lock_key]}
    )
    overlay = rules.data.get("state_overlays", {}).get(state, {})
    if overlay.get("rate_adj"):
        items.append(
            {"rule": f"state_overlays.{state}.rate_adj", "label": f"State: {state}", "adj": overlay["rate_adj"]}
        )

    pts = p["points"]
    if not 0 <= points <= pts["max_points"]:
        raise PricingError(f"points must be between 0 and {pts['max_points']}")
    buydown = points * pts["rate_per_point"]
    if points:
        items.append({"rule": "pricing.points.rate_per_point", "label": f"{points:g} discount points", "adj": -buydown})

    rate = sum(i["adj"] for i in items)
    floor_hit = rate < p["min_rate"]
    rate = max(rate, p["min_rate"])
    return {
        "note_rate": round(rate, 3),
        "items": [{**i, "adj": round(i["adj"], 3)} for i in items],
        "points": points,
        "points_cost": round(loan_amount * points / 100, 2),
        "lock_days": lock_days,
        "min_rate_applied": floor_hit,
    }


def points_ladder(rules: RuleSet, rate_without_points: float, loan_amount: float, monthly_pi_fn) -> list[dict]:
    """Rate, payment, upfront cost and breakeven month for 0 to max points, in 0.5 steps."""
    pts = rules["pricing"]["points"]
    floor = rules["pricing"]["min_rate"]
    base_pi = monthly_pi_fn(rate_without_points)
    out = []
    steps = int(pts["max_points"] * 2) + 1
    for i in range(steps):
        points = i / 2
        rate = max(floor, rate_without_points - points * pts["rate_per_point"])
        pi = monthly_pi_fn(rate)
        cost = loan_amount * points / 100
        saving = base_pi - pi
        out.append(
            {
                "points": points,
                "rate": round(rate, 3),
                "principal_interest": round(pi, 2),
                "upfront_cost": round(cost, 2),
                "monthly_saving": round(saving, 2),
                "breakeven_months": round(cost / saving, 1) if saving > 0 else None,
            }
        )
    return out
