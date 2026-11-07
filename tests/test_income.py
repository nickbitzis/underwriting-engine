import pytest
from conftest import flat_statements, statements

from uwengine.income import bank_statement_income, dscr_worksheet, housing_payment, monthly_pi
from uwengine.models import Borrower, LoanRequest, Property

RULES = {
    "large_deposit_pct": 0.50,
    "expense_factor": {"default": 0.50, "consulting": 0.30, "trucking": 0.70},
}


def borrower(business="consulting"):
    return Borrower("B", 740, business, 5, 0, 0)


def test_flat_business_deposits_apply_expense_factor():
    ws = bank_statement_income(flat_statements(20_000, 12), borrower(), RULES)
    assert ws["eligible_total"] == 240_000
    assert ws["average_monthly_eligible"] == 20_000
    assert ws["expense_factor"] == 0.30
    assert ws["qualifying_monthly_income"] == 14_000
    assert ws["excluded_items"] == []


def test_unknown_business_type_uses_default_factor():
    ws = bank_statement_income(flat_statements(20_000, 12), borrower("florist"), RULES)
    assert ws["expense_factor"] == 0.50
    assert ws["qualifying_monthly_income"] == 10_000


def test_personal_account_has_no_expense_factor():
    st = statements([[(4_000, "personal_deposit"), (4_000, "personal_deposit")]] * 12, account_type="personal")
    ws = bank_statement_income(st, borrower(), RULES)
    assert ws["expense_factor"] == 0
    assert ws["qualifying_monthly_income"] == 8_000


def test_transfers_between_own_accounts_are_excluded():
    months = [[(10_000, "business_deposit"), (10_000, "business_deposit")]] * 12
    months[2] = months[2] + [(7_500, "transfer_in")]
    ws = bank_statement_income(statements(months), borrower(), RULES)
    assert ws["eligible_total"] == 240_000
    assert ws["monthly"][2]["gross_deposits"] == 27_500
    assert ws["monthly"][2]["excluded"] == 7_500
    [item] = ws["excluded_items"]
    assert item["reason"] == "transfer between own accounts" and item["loe"] is False


def test_refunds_are_excluded():
    months = [[(10_000, "business_deposit"), (10_000, "business_deposit")]] * 12
    months[0] = months[0] + [(300, "refund")]
    ws = bank_statement_income(statements(months), borrower(), RULES)
    assert ws["eligible_total"] == 240_000
    assert ws["excluded_items"][0]["reason"] == "refund or reversal"


def test_large_deposit_excluded_and_flagged_with_its_date():
    months = [[(10_000, "business_deposit"), (10_000, "business_deposit")] for _ in range(12)]
    # Month index 4 from 2025-11 is 2026-03; the 5th deposit lands on the 14th.
    months[4] = months[4] + [(1, "business_deposit"), (1, "business_deposit"), (15_000, "business_deposit")]
    ws = bank_statement_income(statements(months), borrower(), RULES)
    [large] = ws["large_deposits"]
    assert large["date"] == "2026-03-14"
    assert large["amount"] == 15_000
    assert large["loe"] is True
    assert ws["large_deposit_threshold"] == pytest.approx((240_002 + 15_000) / 12 * 0.5, abs=0.01)
    assert ws["eligible_total"] == 240_002


def test_deposit_exactly_at_threshold_is_kept():
    # Average is 20,000 a month, threshold 10,000. Deposits of exactly 10,000 are not "above" it.
    ws = bank_statement_income(flat_statements(20_000, 12), borrower(), RULES)
    assert ws["large_deposit_threshold"] == 10_000
    assert ws["large_deposits"] == []


def test_lower_threshold_catches_more_deposits():
    months = [[(5_000, "business_deposit")] * 4 for _ in range(12)]
    months[3] = months[3] + [(9_000, "business_deposit")]
    loose = bank_statement_income(statements(months), borrower(), RULES)
    tight = bank_statement_income(statements(months), borrower(), {**RULES, "large_deposit_pct": 0.40})
    assert loose["large_deposits"] == []
    assert len(tight["large_deposits"]) == 1


def test_24_month_average_counts_empty_months():
    months = [[(20_000, "personal_deposit")]] * 12 + [[]] * 12
    st = statements(months, account_type="personal")
    ws = bank_statement_income(st, borrower(), {**RULES, "large_deposit_pct": 5.0})
    assert ws["months"] == 24
    assert ws["average_monthly_eligible"] == 10_000


def test_worksheet_traces_every_dollar():
    months = [[(9_000, "business_deposit"), (11_000, "business_deposit")] for _ in range(12)]
    months[1] = months[1] + [(4_000, "transfer_in")]
    months[6] = months[6] + [(30_000, "business_deposit")]
    ws = bank_statement_income(statements(months), borrower(), RULES)
    assert sum(r["eligible"] for r in ws["monthly"]) == pytest.approx(ws["eligible_total"])
    for r in ws["monthly"]:
        assert r["gross_deposits"] - r["excluded"] == pytest.approx(r["eligible"])
    assert sum(e["amount"] for e in ws["excluded_items"]) == pytest.approx(sum(r["excluded"] for r in ws["monthly"]))
    assert "240,000.00 / 12 months x (1 - 0.30)" in ws["formula"]


def test_nsf_counted_only_in_last_12_months():
    months = [[(20_000, "business_deposit")]] * 24
    st = statements(months, start="2024-11-01", nsf_dates=["2025-01-15", "2026-02-03", "2026-05-20"])
    ws = bank_statement_income(st, borrower(), {**RULES, "large_deposit_pct": 5.0})
    assert ws["nsf_count_12m"] == 2
    assert [e["date"] for e in ws["nsf_events_12m"]] == ["2026-02-03", "2026-05-20"]


def test_declining_trend_is_measured():
    months = [[(20_000, "business_deposit")]] * 6 + [[(10_000, "business_deposit")]] * 6
    ws = bank_statement_income(statements(months), borrower(), {**RULES, "large_deposit_pct": 5.0})
    assert ws["trend_second_half_vs_first"] == -0.5


def test_transactions_outside_period_are_ignored():
    st = flat_statements(20_000, 12)
    from dataclasses import replace

    from uwengine.models import Transaction

    st = replace(st, transactions=st.transactions + (Transaction("X", "2024-01-05", 99_999, "business_deposit"),))
    ws = bank_statement_income(st, borrower(), RULES)
    assert ws["eligible_total"] == 240_000


def test_monthly_pi_matches_known_value():
    assert monthly_pi(300_000, 7.0, 360) == pytest.approx(1995.91, abs=0.01)
    assert monthly_pi(120_000, 0.0, 360) == pytest.approx(333.33, abs=0.01)


def test_dscr_is_rent_over_pitia():
    prop = Property("x", "TX", "sfr", 400_000, 400_000, 3_000, 6_000, 1_200, 150)
    pay = housing_payment(LoanRequest("dscr", "purchase", 300_000), prop, 7.0, 360)
    assert pay["taxes"] == 500 and pay["insurance"] == 100 and pay["hoa"] == 150
    assert pay["pitia"] == pytest.approx(1995.91 + 750, abs=0.01)
    ws = dscr_worksheet(prop, pay)
    assert ws["dscr"] == pytest.approx(3000 / pay["pitia"], abs=1e-4)
