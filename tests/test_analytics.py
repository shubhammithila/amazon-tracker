"""Portfolio -> Analytics: two years of monthly history for the active range (17 Oct 2026)."""
import ast
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.analytics import fetch, logic, repository
from app.database import async_session
from app.models import AnalyticsMonthly, EconomicsDaily

pytestmark = pytest.mark.regression
ROOT = Path(__file__).resolve().parent.parent


# ── pure arithmetic ─────────────────────────────────────────────────────────────────────────────

def test_month_range_crosses_a_year():
    assert logic.month_range("2024-11", "2025-02") == ["2024-11", "2024-12", "2025-01", "2025-02"]


def size(sales, units, kg):
    return {"sales": sales, "units": units, "kg": kg}


def test_the_growth_split_adds_up_exactly_with_a_pack_in_only_one_month():
    prev = {"A": size(1000.0, 10, 5.0), "B": size(900.0, 3, 6.0)}
    cur = {"A": size(1320.0, 12, 6.0), "C": size(450.0, 1, 2.0)}
    s = logic.growth_split(prev, cur)
    assert s["delta"] == pytest.approx(1770 - 1900)
    assert s["volume"] + s["price"] + s["mix"] == pytest.approx(s["delta"], abs=0.01)
    # A: 100 -> 110 a pack, 12 packs: +120 of price, the only pack in both months.
    assert s["price"] == pytest.approx(120.0)
    # kg 11 -> 8 at last month's 1900/11 per kg.
    assert s["volume"] == pytest.approx(-3 * 1900 / 11, abs=0.01)


def test_a_price_rise_on_an_unchanged_pack_is_price_only():
    s = logic.growth_split({"A": size(1000.0, 10, 5.0)}, {"A": size(1100.0, 10, 5.0)})
    assert s == {"delta": 100.0, "volume": 0.0, "price": 100.0, "mix": 0.0}


def test_a_launch_is_all_volume():
    s = logic.growth_split({}, {"A": size(500.0, 5, 2.5)})
    assert s == {"delta": 500.0, "volume": 500.0, "price": 0.0, "mix": 0.0}


def test_a_shift_to_cheaper_kilograms_shows_as_mix():
    """Same kg and same pack prices, but more of the cheaper-per-kg big pack: mix, not price."""
    prev = {"small": size(600.0, 6, 3.0), "big": size(200.0, 2, 2.0)}       # 200/kg and 100/kg
    cur = {"small": size(400.0, 4, 2.0), "big": size(300.0, 3, 3.0)}         # same 5 kg, cheaper mix
    s = logic.growth_split(prev, cur)
    assert s["price"] == 0.0 and s["volume"] == 0.0
    assert s["mix"] == pytest.approx(s["delta"]) and s["mix"] < 0


def test_an_unknown_weight_falls_back_to_units():
    s = logic.growth_split({"A": size(1000.0, 10, None)}, {"A": size(1500.0, 15, None)})
    assert s["volume"] == pytest.approx(500.0) and s["price"] == 0.0


def test_concentration_is_the_top_n_share():
    sales = {f"p{i}": float(i) for i in range(1, 21)}           # 1..20, total 210
    assert logic.concentration(sales, 10) == pytest.approx(sum(range(11, 21)) / 210)
    assert logic.concentration({}, 10) is None


def test_conversion_comes_from_sums_not_an_average_of_rates():
    rows = [{"sessions": 100, "page_views": 120, "units": 10, "buy_box_pct": 100.0},
            {"sessions": 900, "page_views": 1000, "units": 9, "buy_box_pct": 50.0}]
    r = logic.weighted_rates(rows)
    assert r["conversion"] == pytest.approx(19 / 1000)               # a plain average says 5.5%
    assert r["buy_box"] == pytest.approx((100 * 120 + 50 * 1000) / 1120 / 100)
    assert logic.weighted_rates([]) == {"sessions": None, "page_views": None,
                                        "conversion": None, "buy_box": None}


def test_growth_is_none_without_a_previous_month():
    assert logic.growth(100.0, 0) is None and logic.growth(110.0, 100.0) == pytest.approx(0.1)


# ── fetch and store ─────────────────────────────────────────────────────────────────────────────

def raw(month, asin, sales, units, *, parent="B0PARENT01", fee=100.0, fee_tax=18.0, ads=50.0, net=600.0):
    return {"startDate": f"{month}-01", "parentAsin": parent, "childAsin": asin,
            "sales": {"orderedProductSales": {"amount": sales}, "refundedProductSales": {"amount": 0},
                      "unitsOrdered": units, "unitsRefunded": 0, "netUnitsSold": units},
            "fees": [{"feeTypeName": "Referral", "charges": [{"aggregatedDetail": {
                "totalAmount": {"amount": fee}, "taxAmount": {"amount": fee_tax}}}]}],
            "ads": [{"adTypeName": "SponsoredProductFee", "charge": {"totalAmount": {"amount": ads}}}],
            "netProceeds": {"total": {"amount": net}}}


def test_a_stored_month_is_written_through_the_profit_tabs_rule():
    """Fees ex-GST and the fee GST added back to Net, exactly as `size_row` stores a Profit day."""
    row = repository.rows_from_economics([raw("2025-10", "B0CHILD001", 1000.0, 10)])[0]
    assert row["month"] == "2025-10" and row["ordered_sales"] == 1000.0
    assert row["net_proceeds"] == pytest.approx(618.0)                 # 600 + 18 GST
    assert '"Referral": 82.0' in row["fees_json"]
    assert repository.rows_from_economics([{**raw("2025-10", "X", 1.0, 1), "startDate": None}]) == []


async def test_the_economics_query_asks_for_months():
    seen = {}

    async def run(query, label):
        seen["q"] = query
        return []
    await fetch.economics_months("2025-01-01", "2025-03-31", run=run)
    assert "date: MONTH" in seen["q"] and "date: RANGE" not in seen["q"]


def test_the_current_month_ends_yesterday():
    from datetime import date
    assert fetch.month_bounds("2026-10", date(2026, 10, 17)) == ("2026-10-01", "2026-10-16")
    assert fetch.month_bounds("2026-10", date(2026, 10, 1)) is None
    assert fetch.month_bounds("2026-09", date(2026, 10, 17)) == ("2026-09-01", "2026-09-30")


async def test_saving_a_month_replaces_it_and_keeps_the_others(db_schema):
    async with async_session() as db:
        await repository.save_economics(db, repository.rows_from_economics(
            [raw("2025-09", "B0A", 100.0, 1), raw("2025-10", "B0A", 200.0, 2)]), ["2025-09", "2025-10"])
        await repository.save_economics(db, repository.rows_from_economics(
            [raw("2025-10", "B0A", 300.0, 3)]), ["2025-10"])
        await db.commit()
        rows = {(r.month, float(r.ordered_sales)) for r in (await db.execute(select(AnalyticsMonthly))).scalars()}
    assert rows == {("2025-09", 100.0), ("2025-10", 300.0)}


async def test_sponsored_brands_is_added_to_ads_and_taken_off_net_where_held(db_schema):
    async with async_session() as db:
        await repository.save_economics(db, repository.rows_from_economics(
            [raw("2026-09", "B0A", 1000.0, 10)]), ["2026-09"])
        db.add(EconomicsDaily(day="2026-09-05", child_asin="B0A", seller_sku="", sb_spend=40,
                              sb_basis="declared", ordered_sales=0, net_proceeds=0))
        await db.commit()
        months = await repository.load_months(db)
    row = months["2026-09"][0]
    ads = {a["adTypeName"]: a["charge"]["totalAmount"]["amount"] for a in row["ads"]}
    assert sum(ads.values()) == pytest.approx(90.0)                    # 50 SP + 40 SB
    assert row["netProceeds"]["total"]["amount"] == pytest.approx(618.0 - 40)


@pytest.mark.parametrize("module", ["fetch", "repository", "logic"])
def test_analytics_never_writes_to_amazon(module):
    tree = ast.parse((ROOT / "app" / "analytics" / f"{module}.py").read_text(encoding="utf-8"))
    names = ({n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
             | {a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) for a in n.names})
    assert not names & {"_post", "save_economics_daily", "apply_changes", "create_inbound_plan"}
