import random

import pytest
from conftest import NOW, as_loan_file, bs_input, statements

from uwengine import __version__
from uwengine.engine import EngineError, decide, order_reasons, what_if
from uwengine.models import Reason


def run(inp, rules):
    return decide(inp, rules, decision_id="t", now=NOW)


def test_clean_file_approves_and_records_rules(rules):
    d = run(bs_input(), rules)
    assert d.outcome == "approve"
    assert d.rules_version == "2026.11.0"
    assert d.rules_sha256 == rules.sha256
    assert d.engine_version == __version__
    assert len(d.input_sha256) == 64
    assert d.checks and all(c.passed for c in d.checks)
    assert d.pricing["offered"] is True


def test_large_deposit_becomes_dated_condition(rules):
    months = [[(10_000, "business_deposit")] * 3 for _ in range(12)]
    months[4] = months[4] + [(1, "business_deposit"), (40_000, "business_deposit")]  # lands on 2026-03-14
    d = run(bs_input(st=statements(months)), rules)
    assert d.outcome == "approve_with_conditions"
    [c] = [c for c in d.conditions if c.code == "LOE_LARGE_DEPOSIT"]
    assert "2026-03-14" in c.text and "$40,000.00" in c.text
    assert c.ref.startswith("T")


def test_adverse_reasons_follow_priority_not_check_order(rules):
    # Checked in the order LTV, time in business, reserves. Reported by priority: LTV 6, reserves 8, time 10.
    d = run(bs_input(amount=480_000, liquid=1_000, years=1.0), rules)
    assert d.outcome == "decline"
    failed = [c.rule for c in d.checks if not c.passed]
    assert failed.index("income.min_years_self_employed") < failed.index("products.bank_statement.min_reserves_months")
    assert [r.code for r in d.adverse_action_reasons] == ["LTV", "RESERVES", "TIME_IN_BUSINESS"]


def test_adverse_reasons_capped_at_four(rules):
    d = run(bs_input(state="ND", fico=590, amount=149_000, price=150_000, value=150_000, liquid=0, years=1.0), rules)
    assert len(d.adverse_action_reasons) == 4
    assert d.adverse_action_reasons[0].code == "STATE_INELIGIBLE"
    failed = [c for c in d.checks if not c.passed and c.outcome_if_failed == "decline"]
    assert len(failed) > 4  # the audit record keeps every failed check


def test_order_reasons_is_stable_for_any_input_order():
    rs = [Reason(c, p, c) for c, p in [("A", 5), ("B", 1), ("C", 3), ("D", 2), ("E", 9)]]
    for seed in range(5):
        shuffled = rs[:]
        random.Random(seed).shuffle(shuffled)
        assert [r.code for r in order_reasons(shuffled, 4)] == ["B", "D", "C", "A"]


def test_reason_text_is_plain_language_with_numbers(rules):
    d = run(bs_input(fico=604, amount=300_000), rules)
    text = d.adverse_action_reasons[0].text
    assert "604" in text and "620" in text
    assert "{" not in text


def test_no_ltv_reason_below_the_credit_grid(rules):
    d = run(bs_input(fico=600, amount=300_000), rules)
    assert [r.code for r in d.adverse_action_reasons] == ["CREDIT_SCORE"]


def test_decline_price_is_not_offered(rules):
    d = run(bs_input(fico=600, amount=300_000), rules)
    assert d.pricing["offered"] is False


def test_same_input_same_decision(rules):
    a, b = run(bs_input(), rules), run(bs_input(), rules)
    assert a.to_dict() == b.to_dict()


def test_engine_refuses_a_loan_file(rules):
    with pytest.raises(EngineError, match="EngineInput"):
        decide(as_loan_file(bs_input()), rules)


def test_bank_statement_program_needs_statements(rules):
    from dataclasses import replace

    with pytest.raises(EngineError):
        decide(replace(bs_input(), statements=None), rules)


def test_what_if_down_payment_moves_ltv_and_rate(rules):
    rows = what_if(bs_input(), rules, [{"down_payment_pct": x} for x in (0.05, 0.10, 0.20, 0.30)])
    ltvs = [r["ltv"] for r in rows]
    assert ltvs == sorted(ltvs, reverse=True)
    assert rows[0]["outcome"] == "decline" and "LTV" in rows[0]["reasons"]
    assert rows[-1]["outcome"] == "approve"
    assert rows[-1]["note_rate"] < rows[0]["note_rate"]


def test_what_if_lock_and_points(rules):
    rows = what_if(bs_input(), rules, [{"lock_days": 30}, {"lock_days": 60}, {"points": 1}])
    assert rows[1]["note_rate"] - rows[0]["note_rate"] == pytest.approx(0.25)
    assert rows[0]["note_rate"] - rows[2]["note_rate"] == pytest.approx(0.25)


def test_what_if_rejects_unknown_fields(rules):
    with pytest.raises(EngineError, match="unknown"):
        what_if(bs_input(), rules, [{"credit_score": 800}])


def test_what_if_reports_bad_lock_as_error_row(rules):
    [row] = what_if(bs_input(), rules, [{"lock_days": 90}])
    assert "lock period" in row["error"]
