import pytest
from conftest import CANDIDATE, CURRENT, as_loan_file, bs_input, dscr_input

from uwengine.cli import main
from uwengine.fairlending import fair_lending_report, two_proportion_p
from uwengine.impact import diff_rules, portfolio_fair_lending, render_markdown
from uwengine.synth import generate_portfolio

# ---------- diff-rules ----------


def test_diff_against_itself_changes_nothing(rules):
    files = generate_portfolio(40)
    r = diff_rules(files, rules, rules)
    assert r["changed"] == 0 and r["transitions"] == []
    assert r["repriced_approvals"] == 0


def test_candidate_floor_flips_a_620s_borrower(rules, candidate):
    lf = as_loan_file(bs_input(fico=630, amount=300_000))
    r = diff_rules([lf], rules, candidate)
    assert r["changed"] == 1
    [ch] = r["changes"]
    assert (ch["from"], ch["to"]) == ("approve", "decline")
    assert "CREDIT_SCORE" in ch["new_reasons"]


def test_candidate_dscr_minimum_sends_thin_coverage_to_refer(rules, candidate):
    # DSCR around 1.05: approve under 1.00 minimum, refer under 1.10.
    lf = as_loan_file(dscr_input(rent=2_750))
    r = diff_rules([lf], rules, candidate)
    assert r["changes"][0]["to"] == "refer"


def test_markdown_report_has_counts_and_versions(rules, candidate):
    r = diff_rules(generate_portfolio(60), rules, candidate)
    md = render_markdown(r)
    assert "2026.11.0 to 2027.01.0" in md
    assert "| Outcome | Current | Candidate | Change |" in md
    assert sum(r["outcomes_before"].values()) == sum(r["outcomes_after"].values()) == 60


def test_cli_diff_writes_github_step_summary(tmp_path, monkeypatch, capsys):
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    main(["diff-rules", "--current", str(CURRENT), "--candidate", str(CANDIDATE), "--n", "30", "--step-summary"])
    assert "Rules impact" in summary.read_text()
    assert "Rules impact" in capsys.readouterr().out


def test_cli_diff_fail_over_gate(monkeypatch):
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    with pytest.raises(SystemExit) as e:
        main(
            ["diff-rules", "--current", str(CURRENT), "--candidate", str(CANDIDATE), "--n", "60", "--fail-over", "0.0"]
        )
    assert e.value.code == 1


# ---------- fair lending ----------


def rows(group, approved, declined, referred=0, rate=8.0, fico=740, ltv=0.7, program="bank_statement"):
    base = dict(group=group, note_rate=rate, fico=fico, ltv=ltv, program=program)
    return (
        [{**base, "outcome": "approve"}] * approved
        + [{**base, "outcome": "decline"}] * declined
        + [{**base, "outcome": "refer"}] * referred
    )


def test_air_under_four_fifths_raises_alert():
    rep = fair_lending_report(rows("a", 80, 20) + rows("b", 60, 40))
    assert rep["reference_group"] == "a"
    assert rep["groups"]["b"]["air"] == 0.75
    assert [a["type"] for a in rep["alerts"]] == ["approval_rate_ratio"]
    assert "Not a legal conclusion" in rep["note"]


def test_air_exactly_four_fifths_is_not_an_alert():
    rep = fair_lending_report(rows("a", 80, 20) + rows("b", 64, 36))
    assert rep["groups"]["b"]["air"] == 0.8
    assert rep["alerts"] == []


def test_refers_are_left_out_of_approval_rate():
    rep = fair_lending_report(rows("a", 50, 50, referred=40))
    g = rep["groups"]["a"]
    assert g["decided"] == 100 and g["referred"] == 40 and g["approval_rate"] == 0.5


def test_pricing_disparity_within_matched_bands():
    data = (
        rows("a", 50, 0, rate=8.0, fico=760)
        + rows("a", 50, 0, rate=9.0, fico=630)
        + rows("b", 10, 0, rate=8.25, fico=760)
        + rows("b", 90, 0, rate=9.25, fico=630)
    )
    rep = fair_lending_report(data)
    b = rep["groups"]["b"]
    # Raw gap mixes credit mix (b has more 630s) with price. Matched gap is the 25 bps per band.
    assert b["raw_rate_diff_bps"] == pytest.approx(65.0)
    assert b["adjusted_rate_diff_bps"] == pytest.approx(25.0)
    assert any(a["type"] == "pricing_disparity" for a in rep["alerts"])


def test_two_proportion_p_value():
    assert two_proportion_p(50, 100, 50, 100) == 1.0
    assert two_proportion_p(80, 100, 50, 100) < 0.001


def test_portfolio_report_runs_on_seeded_data(rules):
    rep = portfolio_fair_lending(generate_portfolio(120), rules)
    assert set(rep["groups"]) == {"group_a", "group_b", "group_c"}
    assert sum(g["applications"] for g in rep["groups"].values()) == 120
