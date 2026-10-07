"""Hand-built customer histories with known answers. Values chosen UNEQUAL so a wrong field fails."""
from datetime import date, timedelta

import pytest

from app.repeat import logic

pytestmark = pytest.mark.regression
AS_OF = date(2026, 10, 1)
P30 = logic.period(30, AS_OF)[0]          # 2026-08-03: first day of the 30-day cohort period
HIST = date(2025, 1, 1)
BRAND = {"CS": "Mithila Foods", "JS": "Mithila Foods", "HF": "Howrah Foods"}


def orders(*specs):
    """spec = (buyer, day offset from P30, products...) -> one order each."""
    lines = []
    for i, (buyer, off, *prods) in enumerate(specs):
        for p in prods:
            lines.append({"amazon_order_id": f"o{i}", "buyer_key": buyer,
                          "day": P30 + timedelta(days=off), "parent_asin": p})
    return logic.build_orders(lines)


def run(*specs, history=HIST):
    return logic.metrics(orders(*specs), BRAND, AS_OF, history)


def test_the_cohort_period_is_the_30_days_ending_N_days_before_as_of():
    assert logic.period(30, AS_OF) == (date(2026, 8, 3), date(2026, 9, 1))
    assert logic.period(90, AS_OF) == (date(2026, 6, 4), date(2026, 7, 3))


def test_a_reorder_on_day_20_is_a_30_day_repeat():
    m = run(("a", 0, "CS"), ("a", 20, "CS"))
    assert m["parents"]["CS"][30] == {"buyers": 1, "same": 1, "came_from": 0, "went_on": 0}


def test_a_reorder_on_day_31_is_NOT_a_30_day_repeat_but_is_a_60_day_one():
    m = run(("a", -30, "CS"), ("a", 1, "CS"))
    # 60-day period starts 30 days before P30; anchored at day -30, the reorder is 31 days later.
    assert m["parents"]["CS"][60]["same"] == 1
    m30 = run(("a", 0, "CS"), ("a", 31, "CS"))
    assert m30["parents"]["CS"][30]["same"] == 0


def test_a_second_order_the_SAME_day_is_not_a_repeat():
    m = run(("a", 0, "CS"), ("a", 0, "CS"))
    assert m["parents"]["CS"][30]["same"] == 0


def test_the_anchor_is_the_FIRST_purchase_in_the_period_so_one_customer_counts_once():
    m = run(("a", 0, "CS"), ("a", 5, "CS"), ("a", 10, "CS"))
    assert m["parents"]["CS"][30]["buyers"] == 1 and m["parents"]["CS"][30]["same"] == 1


def test_the_anchor_is_the_first_NOT_the_last_purchase():
    # First buy day 0, again day 29 (still in period). Anchored at day 0, day 29 is a repeat.
    # Anchored at day 29 instead, nothing follows within 30 days and the repeat is lost.
    m = run(("a", 0, "CS"), ("a", 29, "CS"))
    assert m["parents"]["CS"][30]["same"] == 1


def test_chana_then_jau_is_a_CAME_FROM_on_jau_and_a_WENT_ON_on_chana():
    m = run(("a", 0, "CS"), ("a", 12, "JS"))
    assert m["parents"]["JS"][30]["came_from"] == 1 and m["parents"]["JS"][30]["same"] == 0
    assert m["parents"]["CS"][30]["went_on"] == 1
    assert m["flows"]["JS"][30]["came_from"] == [("CS", 1)]
    assert m["flows"]["CS"][30]["went_on"] == [("JS", 1)]


def test_two_products_in_ONE_order_are_a_basket_not_a_cross_flow():
    m = run(("a", 0, "CS", "JS"))
    assert m["parents"]["JS"][30]["came_from"] == 0 and m["parents"]["CS"][30]["went_on"] == 0
    assert m["basket"]["CS"]["with"] == [("JS", 1)] and m["basket"]["CS"]["multi"] == 1


def test_the_brand_total_counts_UNIQUE_customers_not_the_sum_of_rows():
    m = run(("a", 0, "CS"), ("a", 1, "JS"), ("b", 2, "CS"))
    rows = m["parents"]["CS"][30]["buyers"] + m["parents"]["JS"][30]["buyers"]
    assert rows == 3 and m["brands"]["Mithila Foods"][30]["buyers"] == 2
    assert m["brands"]["Mithila Foods"][30]["repeat"] == 1        # a came back on day 1


def test_a_different_brand_does_not_count_as_a_brand_repeat():
    m = run(("a", 0, "CS"), ("a", 5, "HF"))
    assert m["brands"]["Mithila Foods"][30]["repeat"] == 0


def test_a_purchase_outside_the_period_does_not_join_the_cohort():
    m = run(("a", -1, "CS"), ("a", 30, "CS"))   # day before and day after the 30-day period
    assert 30 not in m["parents"].get("CS", {})


def test_a_window_without_enough_history_is_UNAVAILABLE_with_the_reason():
    m = run(("a", 0, "CS"), history=date(2026, 7, 1))
    assert m["windows"][30]["available"] is True
    assert m["windows"][90]["available"] is False
    assert "2026-03-06" in m["windows"][90]["reason"]           # 90-day period start minus 90
    assert 90 not in m["parents"]["CS"]


def test_pct_is_None_below_the_minimum_cohort_and_a_ratio_above():
    assert logic.pct(5, logic.MIN_COHORT - 1) is None
    assert logic.pct(5, 50) == 0.1


def test_flows_are_ordered_by_count_then_id_so_two_renders_agree():
    specs = [("a", 0, "CS"), ("a", 3, "JS"), ("b", 0, "CS"), ("b", 3, "HF"),
             ("c", 0, "CS"), ("c", 3, "HF")]
    assert run(*specs)["flows"]["CS"][30]["went_on"] == [("HF", 2), ("JS", 1)]


def test_the_basket_only_covers_the_last_90_days():
    m = logic.metrics(logic.build_orders([
        {"amazon_order_id": "old", "buyer_key": "a", "day": AS_OF - timedelta(days=90), "parent_asin": "CS"},
        {"amazon_order_id": "new", "buyer_key": "a", "day": AS_OF - timedelta(days=89), "parent_asin": "CS"},
    ]), BRAND, AS_OF, HIST)
    assert m["basket"]["CS"]["orders"] == 1


def test_covered_from_merges_adjacent_windows_and_stops_at_a_gap():
    runs = [("2026-01-01", "2026-01-30"), ("2026-01-31", "2026-03-01"), ("2026-03-10", "2026-10-01")]
    assert logic.covered_from(runs, date(2026, 9, 1)) == date(2026, 3, 10)
    assert logic.covered_from(runs[:2], date(2026, 2, 15)) == date(2026, 1, 1)
    assert logic.covered_from(runs, date(2026, 3, 5)) is None


def test_name_parents_uses_the_shared_name_and_disambiguates_a_DERIVED_collision():
    names = logic.name_parents({
        "P1": ["Peri Peri Roasted Chana", "Nimbu Pudina Roasted Chana"],
        "P2": ["Roasted Chana"],
        "P3": ["Chana Sattu"],
    })
    assert names == {"P1": "Roasted Chana (2 flavours)", "P2": "Roasted Chana", "P3": "Chana Sattu"}


def test_the_history_check_is_exact_at_the_boundary():
    """History must reach period_start - N exactly: one day short is unavailable, exact is not."""
    need = logic.period(90, AS_OF)[0] - timedelta(days=90)
    assert logic.window_status(90, AS_OF, need) == (True, None)
    assert logic.window_status(90, AS_OF, need + timedelta(days=1))[0] is False
