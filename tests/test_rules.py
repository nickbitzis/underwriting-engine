"""Rule loading, versioning, and every edge of the decision tables."""

import shutil

import pytest
import yaml
from conftest import CURRENT, NOW, ROOT, bs_input, dscr_input, flat_statements

from uwengine.engine import decide
from uwengine.rules import RulesError, load_all, load_rules, select_version


def codes(d):
    return [r.code for r in d.adverse_action_reasons]


def run(inp, rules):
    return decide(inp, rules, decision_id="t", now=NOW)


# ---------- loading and versioning ----------


def test_rules_file_loads_with_version_and_hash(rules):
    assert rules.version == "2026.11.0"
    assert str(rules.effective_date) == "2026-11-01"
    assert len(rules.sha256) == 64


def test_load_all_sorts_by_effective_date_and_skips_candidates():
    sets = load_all(ROOT / "rules")
    assert [s.version for s in sets] == ["2026.07.0", "2026.11.0"]


@pytest.mark.parametrize(
    "as_of,version",
    [
        ("2026-07-01", "2026.07.0"),
        ("2026-10-31", "2026.07.0"),
        ("2026-11-01", "2026.11.0"),
        ("2027-06-01", "2026.11.0"),
    ],
)
def test_select_version_by_effective_date(as_of, version):
    assert select_version(load_all(ROOT / "rules"), as_of).version == version


def test_no_rules_before_first_effective_date():
    with pytest.raises(RulesError):
        select_version(load_all(ROOT / "rules"), "2026-06-30")


def _write_variant(tmp_path, mutate):
    data = yaml.safe_load(CURRENT.read_text())
    mutate(data)
    p = tmp_path / "bad.yaml"
    p.write_text(yaml.safe_dump(data))
    return p


def test_llpa_row_with_wrong_width_is_rejected(tmp_path):
    p = _write_variant(tmp_path, lambda d: d["pricing"]["llpa_grid"][0]["adj"].pop())
    with pytest.raises(RulesError, match="cells"):
        load_rules(p)


def test_unsorted_ltv_bands_are_rejected(tmp_path):
    p = _write_variant(tmp_path, lambda d: d["pricing"]["ltv_bands"].reverse())
    with pytest.raises(RulesError, match="ascending"):
        load_rules(p)


def test_missing_section_is_rejected(tmp_path):
    p = _write_variant(tmp_path, lambda d: d.pop("adverse_action"))
    with pytest.raises(RulesError, match="missing"):
        load_rules(p)


def test_duplicate_versions_are_rejected(tmp_path):
    shutil.copy(CURRENT, tmp_path / "a.yaml")
    shutil.copy(CURRENT, tmp_path / "b.yaml")
    with pytest.raises(RulesError, match="duplicate"):
        load_all(tmp_path)


def test_hash_changes_with_content(tmp_path, rules):
    p = _write_variant(tmp_path, lambda d: d.update(description="edited"))
    assert load_rules(p).sha256 != rules.sha256


# ---------- table edges ----------


def test_min_fico_edge(rules):
    assert "CREDIT_SCORE" not in codes(run(bs_input(fico=620, amount=300_000), rules))
    assert "CREDIT_SCORE" in codes(run(bs_input(fico=619, amount=300_000), rules))


def test_dscr_min_fico_edge(rules):
    assert "CREDIT_SCORE" not in codes(run(dscr_input(fico=660), rules))
    assert "CREDIT_SCORE" in codes(run(dscr_input(fico=659), rules))


BS_LTV = [
    (row["fico_min"], purpose, row[purpose])
    for row in yaml.safe_load(CURRENT.read_text())["products"]["bank_statement"]["max_ltv"]
    for purpose in ("purchase", "rate_term", "cash_out")
]


@pytest.mark.parametrize("fico,purpose,max_ltv", BS_LTV)
def test_every_max_ltv_cell_edge(rules, fico, purpose, max_ltv):
    value = 500_000.0
    at = bs_input(fico=fico, purpose=purpose, value=value, price=value, amount=value * max_ltv)
    over = bs_input(fico=fico, purpose=purpose, value=value, price=value, amount=value * max_ltv + 1_000)
    assert "LTV" not in codes(run(at, rules))
    assert "LTV" in codes(run(over, rules))


def test_purchase_ltv_uses_lower_of_price_and_value(rules):
    d = run(bs_input(amount=400_000, price=500_000, value=480_000), rules)
    assert d.metrics["value_basis"] == 480_000
    assert d.metrics["ltv"] == pytest.approx(400_000 / 480_000, abs=1e-4)


def test_jumbo_reduces_max_ltv(rules):
    d = run(bs_input(amount=1_600_000, price=2_000_000, value=2_000_000, per_month=200_000, liquid=1_000_000), rules)
    assert d.metrics["max_ltv"] == pytest.approx(0.85)
    assert any(c.code == "SECOND_APPRAISAL" for c in d.conditions)


def test_loan_amount_limits(rules):
    assert "LOAN_AMOUNT" not in codes(run(bs_input(amount=150_000, price=300_000, value=300_000), rules))
    assert "LOAN_AMOUNT" in codes(run(bs_input(amount=149_000, price=300_000, value=300_000), rules))
    big = bs_input(amount=3_001_000, price=5_000_000, value=5_000_000, per_month=400_000, liquid=3_000_000)
    assert "LOAN_AMOUNT" in codes(run(big, rules))


def _income_for_dti(rules, dti, debts=500.0):
    """Monthly deposits (consulting, 30% expense) that give the target DTI."""
    pitia = run(bs_input(debts=debts), rules).metrics["pitia"]
    return (debts + pitia) / dti / 0.70


@pytest.mark.parametrize("dti,expected", [(0.49, "approve"), (0.52, "refer"), (0.56, "decline")])
def test_dti_edges(rules, dti, expected):
    d = run(bs_input(per_month=_income_for_dti(rules, dti)), rules)
    assert d.metrics["dti"] == pytest.approx(dti, abs=0.002)
    assert d.outcome == expected
    if expected == "decline":
        assert codes(d) == ["DTI"]


def test_reserves_edge(rules):
    base = run(bs_input(liquid=10_000_000), rules)
    cash = base.metrics["cash_to_close"]
    pitia = base.metrics["pitia"]
    ok = run(bs_input(liquid=cash + 3 * pitia + 1), rules)
    short = run(bs_input(liquid=cash + 3 * pitia - 1), rules)
    assert "RESERVES" not in codes(ok)
    assert "RESERVES" in codes(short)


def test_dscr_needs_six_months_reserves(rules):
    d = run(dscr_input(liquid=10_000_000), rules)
    assert d.metrics["required_reserves_months"] == 6


@pytest.mark.parametrize(
    "nsf,outcome",
    [
        (0, "approve"),
        (1, "approve_with_conditions"),
        (3, "approve_with_conditions"),
        (4, "refer"),
        (7, "refer"),
        (8, "decline"),
    ],
)
def test_nsf_thresholds(rules, nsf, outcome):
    dates = [f"2026-{m:02d}-05" for m in range(1, 11)][:nsf]
    d = run(bs_input(st=flat_statements(30_000, 12, nsf_dates=dates)), rules)
    assert d.outcome == outcome
    if outcome == "decline":
        assert codes(d) == ["NSF"]


def test_time_in_business_edge(rules):
    assert run(bs_input(years=2.0), rules).outcome == "approve"
    assert codes(run(bs_input(years=1.9), rules)) == ["TIME_IN_BUSINESS"]


def test_ineligible_state(rules):
    assert codes(run(bs_input(state="ND"), rules))[0] == "STATE_INELIGIBLE"


def test_ny_overlay_cuts_ltv_and_adds_rate(rules):
    tx = run(bs_input(state="TX"), rules)
    ny = run(bs_input(state="NY"), rules)
    assert ny.metrics["max_ltv"] == pytest.approx(tx.metrics["max_ltv"] - 0.05)
    assert ny.pricing["note_rate"] == pytest.approx(tx.pricing["note_rate"] + 0.125)


def test_fl_overlay_adds_reserve_months(rules):
    assert run(bs_input(state="FL"), rules).metrics["required_reserves_months"] == 6


def test_declining_deposits_refer(rules):
    from conftest import statements

    st = statements([[(10_000, "business_deposit")] * 4] * 6 + [[(7_000, "business_deposit")] * 4] * 6)
    d = run(bs_input(st=st), rules)
    assert d.outcome == "refer"
    assert "fell 30%" in d.refer_reasons[0]


@pytest.mark.parametrize("ratio,outcome", [(1.0, "approve"), (0.99, "refer"), (0.70, "decline")])
def test_dscr_edges(rules, ratio, outcome):
    from dataclasses import replace

    probe = dscr_input(rent=2_900)  # DSCR between 1.00 and 1.25: no DSCR price adjustment
    pitia = run(probe, rules).metrics["pitia"]
    inp = replace(probe, property=replace(probe.property, market_rent=round(pitia * ratio, 2)))
    d = run(inp, rules)
    assert d.outcome == outcome
    if outcome == "approve":
        assert d.metrics["dscr"] == pytest.approx(1.0, abs=1e-4)
    if outcome == "decline":
        assert codes(d) == ["DSCR"]
