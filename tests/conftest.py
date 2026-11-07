from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from uwengine.models import (
    BankStatements,
    Borrower,
    EngineInput,
    LoanFile,
    LoanRequest,
    Monitoring,
    Property,
    Transaction,
)
from uwengine.rules import load_rules

ROOT = Path(__file__).resolve().parents[1]
CURRENT = ROOT / "rules" / "uw-rules-2026.11.0.yaml"
PREVIOUS = ROOT / "rules" / "uw-rules-2026.07.0.yaml"
CANDIDATE = ROOT / "rules" / "candidate" / "uw-rules-2027.01.0.yaml"
NOW = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)


@pytest.fixture(scope="session")
def rules():
    return load_rules(CURRENT)


@pytest.fixture(scope="session")
def candidate():
    return load_rules(CANDIDATE)


def statements(
    monthly: list[list[tuple[float, str]]],
    account_type="business",
    start="2025-11-01",
    ending_balance=50_000.0,
    nsf_dates=(),
) -> BankStatements:
    """monthly[i] is a list of (amount, type) deposits in month i. Each lands on the 10th, 11th, ..."""
    y, m = int(start[:4]), int(start[5:7])
    txns = []
    n = 0
    for month in monthly:
        for j, (amt, typ) in enumerate(month):
            n += 1
            txns.append(Transaction(f"T{n:04d}", f"{y:04d}-{m:02d}-{10 + j:02d}", amt, typ, ""))
        m += 1
        if m > 12:
            y, m = y + 1, 1
    for d in nsf_dates:
        n += 1
        txns.append(Transaction(f"T{n:04d}", d, -35.0, "nsf_fee", "NSF fee"))
    end_y, end_m = (y, m - 1) if m > 1 else (y - 1, 12)
    return BankStatements(account_type, len(monthly), start, f"{end_y:04d}-{end_m:02d}-28", tuple(txns), ending_balance)


def flat_statements(per_month=20_000.0, months=12, **kw) -> BankStatements:
    return statements([[(per_month / 2, "business_deposit"), (per_month / 2, "business_deposit")]] * months, **kw)


def bs_input(
    fico=760,
    amount=400_000.0,
    price=500_000.0,
    value=500_000.0,
    purpose="purchase",
    state="TX",
    per_month=30_000.0,
    months=12,
    debts=500.0,
    liquid=250_000.0,
    business="consulting",
    years=5.0,
    st=None,
    lock_days=30,
    points=0.0,
    ptype="sfr",
) -> EngineInput:
    return EngineInput(
        id="TEST-1",
        application_date="2026-11-05",
        borrower=Borrower("Test Borrower", fico, business, years, debts, liquid),
        property=Property(
            "1 Test St", state, ptype, value, price if purpose == "purchase" else None, 3000.0, 6000.0, 1800.0, 0.0
        ),
        loan=LoanRequest("bank_statement", purpose, amount, lock_days, points),
        statements=st or flat_statements(per_month, months),
    )


def dscr_input(
    fico=760, amount=300_000.0, value=450_000.0, rent=3_500.0, purpose="purchase", state="TX", liquid=200_000.0
) -> EngineInput:
    return EngineInput(
        id="TEST-D",
        application_date="2026-11-05",
        borrower=Borrower("Test Investor", fico, "other", 10.0, 800.0, liquid),
        property=Property(
            "2 Test Ave", state, "sfr", value, value if purpose == "purchase" else None, rent, 4800.0, 1200.0, 0.0
        ),
        loan=LoanRequest("dscr", purpose, amount, 30, 0.0),
    )


def as_loan_file(inp: EngineInput, group="group_a") -> LoanFile:
    return LoanFile(
        inp.id, inp.application_date, inp.borrower, inp.property, inp.loan, inp.statements, Monitoring(group)
    )
