"""Seeded synthetic loan files for Kestrel Ridge Lending (made up).

Each file is generated from its own Random(f"{seed}-{i}"), so file 17 is the same whether you
generate 20 files or 2,000. The protected-class monitoring attribute is drawn independently of
every underwriting input.
"""

from __future__ import annotations

import calendar
import math
import random
from datetime import date, timedelta

from .models import (
    BankStatements,
    Borrower,
    LoanFile,
    LoanRequest,
    Monitoring,
    Property,
    Transaction,
)

FIRST = [
    "Avery",
    "Jordan",
    "Riley",
    "Casey",
    "Morgan",
    "Quinn",
    "Rowan",
    "Emerson",
    "Hayden",
    "Parker",
    "Reese",
    "Sawyer",
    "Dakota",
    "Finley",
    "Harper",
    "Kendall",
    "Logan",
    "Marlow",
    "Sloane",
    "Tatum",
]
LAST = [
    "Alder",
    "Brightwater",
    "Calloway",
    "Draycott",
    "Ellery",
    "Fairbanks",
    "Garrow",
    "Holloway",
    "Ingram",
    "Jessup",
    "Kittredge",
    "Larkin",
    "Merriweather",
    "Northcott",
    "Oakes",
    "Pembrook",
    "Quill",
    "Ravenell",
    "Stroud",
    "Thorne",
]
STREETS = ["Juniper", "Sycamore", "Harbor", "Mesa", "Ridgeview", "Copper", "Lantern", "Willow", "Granite", "Bayberry"]
STATES = [
    ("CA", 18),
    ("TX", 16),
    ("FL", 14),
    ("AZ", 8),
    ("GA", 7),
    ("NC", 7),
    ("CO", 6),
    ("NY", 6),
    ("NJ", 5),
    ("WA", 5),
    ("NV", 4),
    ("TN", 3),
    ("ND", 1),
]
BUSINESS = [
    ("consulting", 18),
    ("software", 8),
    ("real_estate_agent", 14),
    ("contractor", 20),
    ("trucking", 12),
    ("restaurant", 8),
    ("retail", 10),
    ("other", 10),
]
EXPENSE = {
    "consulting": 0.30,
    "software": 0.30,
    "real_estate_agent": 0.40,
    "contractor": 0.60,
    "trucking": 0.70,
    "restaurant": 0.75,
    "retail": 0.65,
    "other": 0.50,
}
CUSTOMERS = ["Invoice pmt", "ACH credit", "Card settlement", "Mobile deposit", "Zelle from client", "Wire in"]
GROUPS = [("group_a", 50), ("group_b", 30), ("group_c", 20)]
END_DATE = date(2026, 11, 7)


def _pick(rng: random.Random, weighted):
    items, weights = zip(*weighted, strict=True)
    return rng.choices(items, weights=weights)[0]


def _months_back(end_month: date, n: int) -> list[date]:
    out = []
    y, m = end_month.year, end_month.month
    for _ in range(n):
        out.append(date(y, m, 1))
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return list(reversed(out))


def _pmt(amount: float, rate_pct: float, n: int = 360) -> float:
    r = rate_pct / 1200
    return amount * r / (1 - (1 + r) ** -n)


def _statements(
    rng,
    file_no: str,
    app_date: date,
    avg_gross: float,
    account_type: str,
    months: int,
    nsf_n: int,
    large_n: int,
    trend: float,
) -> BankStatements:
    last_full = (app_date.replace(day=1) - timedelta(days=1)).replace(day=1)
    month_starts = _months_back(last_full, months)
    txns: list[Transaction] = []
    seq = 0

    def add(d: date, amount: float, typ: str, desc: str):
        nonlocal seq
        seq += 1
        txns.append(Transaction(f"{file_no}-T{seq:04d}", d.isoformat(), round(amount, 2), typ, desc))

    dep_type = "business_deposit" if account_type == "business" else "personal_deposit"
    nsf_months = set(rng.sample(range(max(0, months - 12), months), k=min(nsf_n, 12)))
    extra_nsf = max(0, nsf_n - 12)
    large_months = set(rng.sample(range(months), k=min(large_n, months)))
    balance = avg_gross * rng.uniform(0.4, 1.2)
    for i, ms in enumerate(month_starts):
        days = calendar.monthrange(ms.year, ms.month)[1]
        factor = (1 + trend * (i / max(1, months - 1) - 0.5)) * rng.uniform(0.88, 1.12)
        target = max(0.0, avg_gross * factor)
        k = rng.randint(5, 12)
        weights = [rng.uniform(0.5, 1.5) for _ in range(k)]
        s = sum(weights)
        month_in = 0.0
        for w in weights:
            amt = target * w / s
            add(ms + timedelta(days=rng.randrange(days)), amt, dep_type, rng.choice(CUSTOMERS))
            month_in += amt
        if rng.random() < 0.35:
            amt = rng.choice([1000, 2500, 5000, 7500, 10000, 15000]) * 1.0
            add(ms + timedelta(days=rng.randrange(days)), amt, "transfer_in", "Transfer from savings x4471")
            month_in += amt
        if rng.random() < 0.10:
            amt = rng.uniform(40, 600)
            add(ms + timedelta(days=rng.randrange(days)), amt, "refund", "Merchant refund")
            month_in += amt
        if i in large_months:
            amt = avg_gross * rng.uniform(0.7, 2.5)
            add(ms + timedelta(days=rng.randrange(days)), amt, dep_type, "Wire in, no reference")
            month_in += amt
        out_total = month_in * rng.uniform(0.85, 1.02)
        w_n = rng.randint(4, 8)
        for _ in range(w_n):
            add(
                ms + timedelta(days=rng.randrange(days)),
                -out_total / w_n,
                "withdrawal",
                rng.choice(["Supplier ACH", "Payroll", "Card purchases", "Rent", "Owner draw", "Fuel"]),
            )
        if i in nsf_months:
            for _ in range(1 + (extra_nsf if i == max(nsf_months) else 0)):
                add(ms + timedelta(days=rng.randrange(days)), -35.0, "nsf_fee", "NSF fee")
        balance += month_in - out_total
    txns.sort(key=lambda t: (t.date, t.id))
    period_end = date(last_full.year, last_full.month, calendar.monthrange(last_full.year, last_full.month)[1])
    return BankStatements(
        account_type,
        months,
        month_starts[0].isoformat(),
        period_end.isoformat(),
        tuple(txns),
        round(max(balance, 500.0), 2),
    )


def generate_file(seed: int, i: int) -> LoanFile:
    rng = random.Random(f"{seed}-{i}")
    file_no = f"KRL-{i + 1:05d}"
    app_date = END_DATE - timedelta(days=rng.randrange(0, 21))
    program = "bank_statement" if rng.random() < 0.62 else "dscr"
    state = _pick(rng, STATES)
    fico = int(min(820, max(560, rng.gauss(712, 48))))
    purpose = _pick(rng, [("purchase", 55), ("rate_term", 20), ("cash_out", 25)])
    ptype = _pick(rng, [("sfr", 70), ("condo", 18), ("2_4_unit", 12)])
    value = round(min(3_500_000, max(140_000, math.exp(rng.gauss(math.log(520_000), 0.45)))), -3)
    price = round(value * rng.uniform(0.97, 1.02), -3) if purpose == "purchase" else None
    basis = min(value, price) if price else value
    # Loan officers mostly structure files near the program limits; about 1 in 10 asks for more.
    cap = 0.90 if fico >= 740 else 0.85 if fico >= 700 else 0.80 if fico >= 660 else 0.75
    cap -= {"purchase": 0.0, "rate_term": 0.05, "cash_out": 0.10}[purpose] + (0.08 if program == "dscr" else 0.0)
    ltv = cap - rng.choice([0.0, 0.0, 0.0, 0.03, 0.05, 0.10, 0.15, 0.25])
    if rng.random() < 0.10:
        ltv += rng.uniform(0.03, 0.08)
    ltv = min(0.95, max(0.3, ltv))
    amount = math.floor(basis * ltv / 1000) * 1000.0
    taxes = round(value * rng.uniform(0.008, 0.019), 2)
    ins_rate = rng.uniform(0.006, 0.011) if state == "FL" else rng.uniform(0.0025, 0.006)
    insurance = round(value * ins_rate, 2)
    hoa = (
        round(rng.uniform(250, 600), 2)
        if ptype == "condo"
        else (round(rng.uniform(0, 90), 2) if rng.random() < 0.2 else 0.0)
    )
    pitia_est = _pmt(amount, 7.9) + taxes / 12 + insurance / 12 + hoa
    if program == "dscr":
        rent = round(pitia_est * min(1.7, max(0.6, rng.gauss(1.12, 0.2))), -1)
    else:
        rent = round(value * rng.uniform(0.0055, 0.0085), -1)

    business = _pick(rng, BUSINESS)
    years = round(rng.uniform(2, 18) if rng.random() < 0.95 else rng.uniform(0.5, 1.9), 1)
    debts = round(rng.uniform(150, 2500), -1)
    statements = None
    if program == "bank_statement":
        account_type = "business" if rng.random() < 0.75 else "personal"
        months = 24 if rng.random() < 0.6 else 12
        target_dti = min(0.65, max(0.18, rng.gauss(0.40, 0.09)))
        income = (debts + pitia_est) / target_dti
        ef = EXPENSE[business] if account_type == "business" else 0.0
        avg_gross = income / (1 - ef)
        r = rng.random()
        nsf_n = (
            0 if r < 0.72 else rng.randint(1, 3) if r < 0.92 else rng.randint(4, 6) if r < 0.975 else rng.randint(8, 10)
        )
        large_n = 0 if rng.random() < 0.7 else rng.randint(1, 2)
        trend = rng.gauss(0.03, 0.12) if rng.random() > 0.06 else rng.uniform(-0.75, -0.55)
        statements = _statements(rng, file_no, app_date, avg_gross, account_type, months, nsf_n, large_n, trend)

    down = (price - amount) if price else 0.0
    floor_months = 4.0 + (6 if program == "dscr" else 0) + (3 if state == "FL" else 0)
    if rng.random() > 0.08:
        reserves_target = max(floor_months, rng.gauss(14, 6))
    else:
        reserves_target = rng.uniform(-2, 2.5)
    liquid = round(max(2_000, down + amount * 0.035 + reserves_target * pitia_est), -2)

    return LoanFile(
        id=file_no,
        application_date=app_date.isoformat(),
        borrower=Borrower(
            name=f"{rng.choice(FIRST)} {rng.choice(LAST)}",
            credit_score=fico,
            business_type=business,
            years_self_employed=years,
            monthly_debts=debts,
            liquid_assets=liquid,
        ),
        property=Property(
            address=f"{rng.randint(100, 9899)} {rng.choice(STREETS)} {rng.choice(['St', 'Ave', 'Ln', 'Ct'])}",
            state=state,
            property_type=ptype,
            appraised_value=float(value),
            purchase_price=float(price) if price else None,
            market_rent=float(rent),
            annual_taxes=taxes,
            annual_insurance=insurance,
            monthly_hoa=hoa,
        ),
        loan=LoanRequest(
            program=program,
            purpose=purpose,
            amount=float(amount),
            lock_days=_pick(rng, [(30, 60), (45, 25), (60, 15)]),
            points=0.0,
        ),
        statements=statements,
        monitoring=Monitoring(protected_group=_pick(rng, GROUPS)),
    )


def generate_portfolio(n: int = 300, seed: int = 7) -> list[LoanFile]:
    return [generate_file(seed, i) for i in range(n)]
