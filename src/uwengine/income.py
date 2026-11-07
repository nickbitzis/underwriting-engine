"""Bank-statement income and DSCR, each returned as a worksheet an underwriter can audit.

Bank-statement income:
  1. Take every credit on the statements.
  2. Exclude transfers between the borrower's own accounts and refunds. They are not income.
  3. Average the remaining deposits per month. Any single deposit above `large_deposit_pct` of
     that average is excluded and flagged for a letter of explanation.
  4. Qualifying monthly income = eligible deposits / months * (1 - expense factor).
     Business accounts use the expense factor for the business type. Personal accounts use 0.

Every excluded item carries its transaction id and reason, and each month row sums to the
eligible total, so any number on the worksheet traces back to deposits.
"""

from __future__ import annotations

from collections import OrderedDict
from datetime import date

from .models import BankStatements, Borrower, LoanRequest, Property

INCOME_TYPES = {"business_deposit", "personal_deposit"}
NEVER_INCOME = {"transfer_in": "transfer between own accounts", "refund": "refund or reversal"}


def _month_keys(start: str, months: int) -> list[str]:
    d = date.fromisoformat(start)
    y, m = d.year, d.month
    keys = []
    for _ in range(months):
        keys.append(f"{y:04d}-{m:02d}")
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return keys


def r2(x: float) -> float:
    return round(x + 0.0, 2)


def bank_statement_income(st: BankStatements, borrower: Borrower, income_rules: dict) -> dict:
    months = _month_keys(st.period_start, st.months)
    rows = OrderedDict((k, {"month": k, "gross_deposits": 0.0, "excluded": 0.0, "eligible": 0.0}) for k in months)
    excluded: list[dict] = []
    candidates = []
    nsf_events = []
    last12 = set(months[-12:])

    for t in st.transactions:
        key = t.date[:7]
        if key not in rows:
            continue
        if t.type == "nsf_fee":
            if key in last12:
                nsf_events.append({"txn_id": t.id, "date": t.date, "amount": t.amount})
            continue
        if t.amount <= 0:
            continue
        rows[key]["gross_deposits"] += t.amount
        if t.type in NEVER_INCOME:
            rows[key]["excluded"] += t.amount
            excluded.append(
                {"txn_id": t.id, "date": t.date, "amount": t.amount, "reason": NEVER_INCOME[t.type], "loe": False}
            )
        elif t.type in INCOME_TYPES:
            candidates.append(t)

    n = st.months
    avg_before = sum(t.amount for t in candidates) / n if n else 0.0
    pct = income_rules["large_deposit_pct"]
    threshold = avg_before * pct
    large = []
    for t in candidates:
        key = t.date[:7]
        if threshold > 0 and t.amount > threshold:
            rows[key]["excluded"] += t.amount
            item = {
                "txn_id": t.id,
                "date": t.date,
                "amount": t.amount,
                "reason": f"large deposit above {pct:.0%} of average monthly deposits (${threshold:,.2f})",
                "loe": True,
            }
            excluded.append(item)
            large.append(item)
        else:
            rows[key]["eligible"] += t.amount

    eligible_total = sum(r["eligible"] for r in rows.values())
    if st.account_type == "business":
        factors = income_rules["expense_factor"]
        expense_factor = factors.get(borrower.business_type, factors["default"])
    else:
        expense_factor = 0.0
    avg_eligible = eligible_total / n if n else 0.0
    qualifying = avg_eligible * (1 - expense_factor)

    half = n // 2
    first = sum(r["eligible"] for r in list(rows.values())[:half])
    second = sum(r["eligible"] for r in list(rows.values())[half:])
    trend = (second - first) / first if first > 0 else 0.0

    for r in rows.values():
        for k in ("gross_deposits", "excluded", "eligible"):
            r[k] = r2(r[k])

    return {
        "method": "bank_statement",
        "account_type": st.account_type,
        "months": n,
        "period": [st.period_start, st.period_end],
        "monthly": list(rows.values()),
        "excluded_items": sorted(excluded, key=lambda e: e["date"]),
        "large_deposits": sorted(large, key=lambda e: e["date"]),
        "large_deposit_pct": pct,
        "large_deposit_threshold": r2(threshold),
        "average_before_large_exclusion": r2(avg_before),
        "eligible_total": r2(eligible_total),
        "average_monthly_eligible": r2(avg_eligible),
        "expense_factor": expense_factor,
        "qualifying_monthly_income": r2(qualifying),
        "nsf_events_12m": nsf_events,
        "nsf_count_12m": len(nsf_events),
        "trend_second_half_vs_first": round(trend, 4),
        "formula": f"{r2(eligible_total):,.2f} / {n} months x (1 - {expense_factor:.2f}) = {r2(qualifying):,.2f}",
    }


def monthly_pi(principal: float, annual_rate_pct: float, term_months: int) -> float:
    """Principal and interest on a fully amortizing fixed-rate loan."""
    r = annual_rate_pct / 100 / 12
    if r == 0:
        return principal / term_months
    return principal * r / (1 - (1 + r) ** -term_months)


def housing_payment(loan: LoanRequest, prop: Property, rate_pct: float, term_months: int) -> dict:
    pi = monthly_pi(loan.amount, rate_pct, term_months)
    taxes = prop.annual_taxes / 12
    ins = prop.annual_insurance / 12
    total = pi + taxes + ins + prop.monthly_hoa
    return {
        "principal_interest": r2(pi),
        "taxes": r2(taxes),
        "insurance": r2(ins),
        "hoa": r2(prop.monthly_hoa),
        "pitia": r2(total),
        "rate_pct": rate_pct,
        "term_months": term_months,
    }


def dscr_worksheet(prop: Property, payment: dict) -> dict:
    pitia = payment["pitia"]
    dscr = prop.market_rent / pitia if pitia else 0.0
    return {
        "method": "dscr",
        "market_rent": r2(prop.market_rent),
        "payment": payment,
        "dscr": round(dscr, 4),
        "formula": f"{prop.market_rent:,.2f} rent / {pitia:,.2f} PITIA = {dscr:.3f}",
    }
