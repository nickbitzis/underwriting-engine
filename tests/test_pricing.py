import pytest
from conftest import NOW, bs_input

from uwengine.engine import decide
from uwengine.income import monthly_pi
from uwengine.pricing import PricingError, ltv_band_index, points_ladder, price


def p(rules, **kw):
    args = dict(
        program="bank_statement",
        fico=745,
        ltv=0.78,
        purpose="purchase",
        property_type="sfr",
        state="TX",
        loan_amount=400_000,
        lock_days=30,
        points=0,
        statement_months=24,
    )
    args.update(kw)
    return price(rules, **args)


def adj(result, prefix):
    return [i["adj"] for i in result["items"] if i["rule"].startswith(prefix)]


def test_llpa_cell_lookup(rules):
    r = p(rules)
    assert adj(r, "pricing.llpa_grid") == [0.375]  # 740 row, 75 to 80 band
    assert r["note_rate"] == pytest.approx(7.125 + 0.375)


def test_ltv_band_upper_bound_is_inclusive(rules):
    bands = rules["pricing"]["ltv_bands"]
    assert ltv_band_index(bands, 0.80) == 3
    assert ltv_band_index(bands, 0.8001) == 4
    assert ltv_band_index(bands, 0.95) is None


def test_items_sum_to_note_rate(rules):
    r = p(rules, purpose="cash_out", property_type="condo", statement_months=12, lock_days=60, state="NJ")
    assert sum(i["adj"] for i in r["items"]) == pytest.approx(r["note_rate"])
    assert all(i["rule"] for i in r["items"])


def test_lock_period_adjustments(rules):
    assert p(rules, lock_days=45)["note_rate"] - p(rules)["note_rate"] == pytest.approx(0.125)
    with pytest.raises(PricingError, match="lock period"):
        p(rules, lock_days=90)


def test_points_buy_down_rate(rules):
    r = p(rules, points=2)
    assert p(rules)["note_rate"] - r["note_rate"] == pytest.approx(0.5)
    assert r["points_cost"] == 8_000


def test_points_above_max_rejected(rules):
    with pytest.raises(PricingError):
        p(rules, points=3.5)


def test_min_rate_floor(rules):
    r = p(rules, fico=800, ltv=0.5, points=3, loan_amount=400_000)
    raw = sum(i["adj"] for i in r["items"])
    if raw < rules["pricing"]["min_rate"]:
        assert r["min_rate_applied"]
    assert r["note_rate"] >= rules["pricing"]["min_rate"]


def test_dscr_band_pricing(rules):
    strong = p(rules, program="dscr", dscr=1.30, statement_months=None)
    weak = p(rules, program="dscr", dscr=0.95, statement_months=None)
    assert weak["note_rate"] - strong["note_rate"] == pytest.approx(0.625)


def test_points_ladder_tradeoff(rules):
    ladder = points_ladder(rules, 8.0, 400_000, lambda r: monthly_pi(400_000, r, 360))
    assert [row["points"] for row in ladder] == [0, 0.5, 1, 1.5, 2, 2.5, 3]
    assert ladder[0]["breakeven_months"] is None
    rates = [row["rate"] for row in ladder]
    assert rates == sorted(rates, reverse=True)
    one = ladder[2]
    assert one["upfront_cost"] == 4_000
    assert one["breakeven_months"] == pytest.approx(4_000 / one["monthly_saving"], abs=0.1)


def test_engine_rate_matches_points_request(rules):
    d0 = decide(bs_input(), rules, now=NOW)
    d2 = decide(bs_input(points=2), rules, now=NOW)
    assert d0.pricing["note_rate"] - d2.pricing["note_rate"] == pytest.approx(0.5)
    assert d2.pricing["ladder"][0]["rate"] == d0.pricing["note_rate"]
