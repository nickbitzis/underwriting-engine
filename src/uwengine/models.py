"""Domain model for a loan file.

A LoanFile is what arrives from the point of sale. It carries a `monitoring` block with a
protected-class attribute that exists only for fair lending monitoring. The engine never sees a
LoanFile: it sees an EngineInput, built by `to_engine_input`, which has no field that could hold
monitoring data. That boundary is enforced by tests.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields, replace
from datetime import date

PROGRAMS = ("bank_statement", "dscr")
PURPOSES = ("purchase", "rate_term", "cash_out")
TXN_TYPES = (
    "business_deposit",  # customer payments, sales, invoices
    "personal_deposit",  # deposits to a personal account, other than transfers
    "transfer_in",  # moved in from another account the borrower owns: never income
    "refund",  # returned purchases, chargebacks reversed: never income
    "withdrawal",
    "nsf_fee",
)


@dataclass(frozen=True)
class Transaction:
    id: str
    date: str  # ISO date
    amount: float  # positive in, negative out
    type: str
    description: str = ""


@dataclass(frozen=True)
class BankStatements:
    account_type: str  # "business" or "personal"
    months: int  # 12 or 24
    period_start: str
    period_end: str
    transactions: tuple[Transaction, ...]
    ending_balance: float


@dataclass(frozen=True)
class Borrower:
    name: str
    credit_score: int
    business_type: str
    years_self_employed: float
    monthly_debts: float  # minimum payments from the credit report, excluding this loan
    liquid_assets: float  # verified checking, savings, brokerage


@dataclass(frozen=True)
class Property:
    address: str
    state: str
    property_type: str  # sfr, condo, 2_4_unit
    appraised_value: float
    purchase_price: float | None
    market_rent: float  # monthly, from the appraisal rent schedule
    annual_taxes: float
    annual_insurance: float
    monthly_hoa: float = 0.0


@dataclass(frozen=True)
class LoanRequest:
    program: str
    purpose: str
    amount: float
    lock_days: int = 30
    points: float = 0.0


@dataclass(frozen=True)
class Monitoring:
    """Collected for fair lending monitoring only. Never an input to a decision."""

    protected_group: str


@dataclass(frozen=True)
class LoanFile:
    id: str
    application_date: str
    borrower: Borrower
    property: Property
    loan: LoanRequest
    statements: BankStatements | None = None
    monitoring: Monitoring | None = None


@dataclass(frozen=True)
class EngineInput:
    """Everything the engine is allowed to see. Deliberately has no monitoring field."""

    id: str
    application_date: str
    borrower: Borrower
    property: Property
    loan: LoanRequest
    statements: BankStatements | None = None

    def with_loan(self, **changes) -> EngineInput:
        return replace(self, loan=replace(self.loan, **changes))


def to_engine_input(loan_file: LoanFile) -> EngineInput:
    names = {f.name for f in fields(EngineInput)}
    return EngineInput(**{n: getattr(loan_file, n) for n in names})


# ---------- (de)serialization ----------


def loan_file_to_dict(lf: LoanFile | EngineInput) -> dict:
    return asdict(lf)


def _txn(d: dict) -> Transaction:
    return Transaction(**d)


def loan_file_from_dict(d: dict) -> LoanFile:
    st = d.get("statements")
    statements = None
    if st:
        statements = BankStatements(
            account_type=st["account_type"],
            months=int(st["months"]),
            period_start=st["period_start"],
            period_end=st["period_end"],
            transactions=tuple(_txn(t) for t in st["transactions"]),
            ending_balance=float(st["ending_balance"]),
        )
    mon = d.get("monitoring")
    loan = d["loan"]
    if loan["program"] not in PROGRAMS:
        raise ValueError(f"unknown program {loan['program']!r}")
    if loan["purpose"] not in PURPOSES:
        raise ValueError(f"unknown purpose {loan['purpose']!r}")
    return LoanFile(
        id=d["id"],
        application_date=d.get("application_date") or date.today().isoformat(),
        borrower=Borrower(**d["borrower"]),
        property=Property(**d["property"]),
        loan=LoanRequest(**loan),
        statements=statements,
        monitoring=Monitoring(**mon) if mon else None,
    )


@dataclass
class Condition:
    code: str
    text: str
    ref: str | None = None  # transaction id or field the condition points at


@dataclass
class Reason:
    code: str
    priority: int
    text: str


@dataclass
class RuleCheck:
    rule: str
    passed: bool
    actual: object
    limit: object
    outcome_if_failed: str  # decline, refer, condition


@dataclass
class Decision:
    decision_id: str
    application_id: str
    outcome: str  # approve, approve_with_conditions, refer, decline
    program: str
    rules_version: str
    rules_sha256: str
    engine_version: str
    decided_at: str
    as_of: str
    adverse_action_reasons: list[Reason] = field(default_factory=list)
    refer_reasons: list[str] = field(default_factory=list)
    conditions: list[Condition] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)
    pricing: dict = field(default_factory=dict)
    income_worksheet: dict | None = None
    dscr_worksheet: dict | None = None
    checks: list[RuleCheck] = field(default_factory=list)
    input_sha256: str = ""

    def to_dict(self) -> dict:
        return asdict(self)
