"""The verdict rules, against REAL Amazon rows.

`tests/fixtures/economics_rows.json` is 16 rows captured from the live amazon.in account on
2026-08-27 through Data Kiosk, chosen to exercise every rule: two products with fewer than three
units, one with a 100% return rate, two losing money at high TACOS, three healthy, two with zero
units, and a whole variation family whose sizes disagree about whether they make money.

**Real rows rather than invented ones, deliberately.** A hand-written fixture encodes what I
believe Amazon sends; these encode what it actually sends. Two of the traps in this feature —
`ads.charge` being an `AggregatedDetail` directly, and a size with sales but zero net units —
would not appear in a fixture I wrote from the schema.
"""
import json
from datetime import date
from pathlib import Path

import pytest

from app.portfolio import logic

pytestmark = pytest.mark.regression

FIXTURES = Path(__file__).parent / "fixtures"
TODAY = date(2026, 8, 27)

#: The catalogue shape `catalogue.load_catalogue()` returns, for the ASINs the fixture uses.
#: Only name/weight/brand matter here; the join itself is covered in the API tests.
CATALOGUE = {
    "B0AAA00001": {"name": "Test Sattu", "weight": 0.5, "brand": "Mithila Foods"},
    "B0AAA00002": {"name": "Test Sattu", "weight": 1.0, "brand": "Mithila Foods"},
    "B0AAA00003": {"name": "Test Sattu", "weight": 2.0, "brand": "Mithila Foods"},
}


def _fixture():
    return json.loads((FIXTURES / "economics_rows.json").read_text(encoding="utf-8"))


def _row(child, parent, *, sales=0.0, ads=0.0, net=0.0, units=0,
         ordered=None, refunded=0, fees=None):
    """An economics row in Amazon's exact nested shape."""
    return {
        "parentAsin": parent,
        "childAsin": child,
        "sales": {
            "orderedProductSales": {"amount": sales, "currencyCode": "INR"},
            "refundedProductSales": {"amount": 0.0, "currencyCode": "INR"},
            "unitsOrdered": units if ordered is None else ordered,
            "unitsRefunded": refunded,
            "netUnitsSold": units,
        },
        "fees": [
            {"feeTypeName": name,
             "charges": [{"aggregatedDetail": {"totalAmount": {"amount": amount}}}]}
            for name, amount in (fees or {}).items()
        ],
        # NOTE the shape: charge -> totalAmount, with NO intervening aggregatedDetail. Writing
        # it the other way (by analogy with fees) is rejected by Amazon as an invalid query.
        "ads": [{"adTypeName": "SponsoredProductFee",
                 "charge": {"totalAmount": {"amount": ads}}}] if ads else [],
        "netProceeds": {"total": {"amount": net}},
    }


# ─── The fixture itself ──────────────────────────────────────────────────────


def test_the_fixture_still_carries_the_cases_that_shaped_the_rules():
    """A guard on every other test in this file.

    If someone re-captures this fixture from a quieter month, the rules below would be asserted
    against input that cannot exercise them — and would pass while proving nothing. This is the
    same guard `test_orders_spapi.py` carries for its order fixtures, and for the same reason.
    """
    rows = _fixture()
    assert len(rows) >= 12, "the fixture has shrunk; it can no longer cover the rules"

    def units(row):
        return int((row.get("sales") or {}).get("netUnitsSold") or 0)

    def ads(row):
        return sum(
            float(((a.get("charge") or {}).get("totalAmount") or {}).get("amount") or 0)
            for a in (row.get("ads") or [])
        )

    assert any(units(r) <= 2 for r in rows), "no low-volume row: the DEAD rule is untested"
    assert any(units(r) == 0 for r in rows), "no zero-unit row: the division guard is untested"
    assert any(ads(r) > 0 for r in rows), "no ad spend: TACOS is untested"
    assert any(len(r.get("fees") or []) >= 4 for r in rows), "no multi-fee row"
    parents = {r.get("parentAsin") for r in rows}
    assert len(parents) >= 4, "too few families to test the parent rollup"


def test_the_real_fixture_produces_a_verdict_for_every_product():
    """No product may render without a verdict — a blank cell reads as a bug, not a judgement."""
    result = logic.portfolio(_fixture(), {}, ratings={}, decisions={}, today=TODAY)
    assert result["parents"], "the fixture produced no products at all"
    for parent in result["parents"]:
        assert parent["verdict"] in logic.VERDICT_ORDER, parent
        assert parent["verdict_reason"], f"{parent['product']} has a verdict with no reason"


# ─── Rule ORDER, which is itself a rule ──────────────────────────────────────


def test_a_product_with_almost_no_volume_is_dead_not_a_best_bet():
    """**The rule-order test that matters most.**

    Built from the real shape that made this necessary: the live data holds a product that sold
    2 units for Rs 608 and reported +505.6% net proceeds, because a refund reversal landed in the
    window. On margin alone it is the best product in the portfolio; it is noise.

    If the DEAD check moves below the economics checks, this row becomes BEST BET and the
    dashboard recommends scaling a product that sells twice a month.
    """
    row = _row("B0AAA00001", "B0PARENT01", sales=608.0, ads=115.0, net=3075.0, units=2)
    result = logic.portfolio([row], CATALOGUE, ratings={}, decisions={}, today=TODAY)
    parent = result["parents"][0]
    assert parent["verdict"] == logic.VERDICT_DEAD, (
        f"a 2-unit product was judged {parent['verdict']} on {parent['net_pct']:.1%} margin — "
        "the volume check must come first, or the dashboard recommends scaling noise"
    )
    assert "2 unit" in parent["verdict_reason"]


def test_returns_outrank_a_healthy_margin():
    """A product a sixth of buyers send back is not a keeper, whatever the margin says.

    Refunds are already netted off `netProceeds`, so a high-return product can show a perfectly
    respectable margin on the units that stayed sold. Money cannot fix a product people reject,
    which is why this check sits above the economics ones.
    """
    row = _row(
        "B0AAA00002", "B0PARENT01",
        sales=50000.0, ads=8000.0, net=20000.0, units=100, ordered=125, refunded=25,
    )
    result = logic.portfolio([row], CATALOGUE, ratings={}, decisions={}, today=TODAY)
    parent = result["parents"][0]
    assert parent["verdict"] == logic.VERDICT_KILL, (
        f"a 20% return rate at 40% margin was judged {parent['verdict']}"
    )
    assert "returned" in parent["verdict_reason"]


def test_a_high_return_rate_on_trivial_volume_is_dead_not_kill():
    """One unit sold and returned is not a quality signal.

    The live data holds exactly this: 1 unit, 1 refund, 100% return rate. Escalating it to KILL
    would put a product with no evidence at the top of the worklist, and the returns rule
    therefore carries a minimum-volume condition.
    """
    row = _row("B0AAA00001", "B0PARENT01", sales=174.0, ads=268.0, net=-125.0,
               units=1, ordered=1, refunded=1)
    result = logic.portfolio([row], CATALOGUE, ratings={}, decisions={}, today=TODAY)
    assert result["parents"][0]["verdict"] == logic.VERDICT_DEAD


# ─── The money rules ─────────────────────────────────────────────────────────


def test_losing_money_on_heavy_ads_is_a_kill():
    """The measured Moori case: -56.8% net at 78% TACOS.

    This is the verdict that reproduces the conclusion reached by hand from the Business Report,
    which is the point of the whole feature — the same answer, without the manual download.
    """
    row = _row("B0AAA00001", "B0PARENT01", sales=32898.0, ads=25660.0, net=-18686.0, units=204,
               ordered=208, refunded=4)
    result = logic.portfolio([row], CATALOGUE, ratings={}, decisions={}, today=TODAY)
    parent = result["parents"][0]
    assert parent["verdict"] == logic.VERDICT_KILL
    assert "TACOS" in parent["verdict_reason"]


def test_a_loss_at_low_ad_spend_is_not_a_kill():
    """AND, not OR — and the distinction is a real decision.

    A negative margin at low TACOS is a PRICING problem worth fixing; a negative margin
    sustained by heavy spend is a product being bought only because it is being paid for. Only
    the second is a kill, so this must fall through to MONITOR.
    """
    row = _row("B0AAA00002", "B0PARENT01", sales=40000.0, ads=2000.0, net=-1500.0, units=150,
               ordered=150)
    result = logic.portfolio([row], CATALOGUE, ratings={}, decisions={}, today=TODAY)
    assert result["parents"][0]["verdict"] == logic.VERDICT_MONITOR


def test_a_healthy_product_is_a_best_bet_and_a_poorly_rated_one_is_scale():
    """The rating is what separates these two, and it changes the ACTION.

    Same economics, different star rating: BEST BET means spend more, SCALE means fix the
    product first. Getting this backwards means pouring ad budget into a listing whose reviews
    are the reason it converts badly.
    """
    row = _row("B0AAA00003", "B0PARENT01", sales=100000.0, ads=25000.0, net=40000.0, units=400,
               ordered=400)

    good = logic.portfolio([row], CATALOGUE,
                           ratings={"B0AAA00003": {"rating": 4.4, "rating_count": 300}},
                           decisions={}, today=TODAY)["parents"][0]
    assert good["verdict"] == logic.VERDICT_BEST_BET

    poor = logic.portfolio([row], CATALOGUE,
                           ratings={"B0AAA00003": {"rating": 3.6, "rating_count": 300}},
                           decisions={}, today=TODAY)["parents"][0]
    assert poor["verdict"] == logic.VERDICT_SCALE
    assert "3.6 stars" in poor["verdict_reason"]


def test_a_profitable_parent_with_a_losing_size_is_surgical():
    """**The Beetroot Sattu shape, and the reason this tab expands to sizes at all.**

    Measured on the live account: the parent earns +6.3% overall while its 0.5 kg pack loses
    30% and its 1 kg and 2 kg packs make 43.6% and 65.8%. Judged at the parent alone it looks
    like a mediocre keeper, and the correct action — kill the small pack, keep the big ones —
    is invisible.
    """
    rows = [
        _row("B0AAA00001", "B0PARENT01", sales=21400.0, ads=17334.0, net=-6420.0, units=80,
             ordered=80),
        _row("B0AAA00002", "B0PARENT01", sales=28900.0, ads=8381.0, net=12600.0, units=60,
             ordered=60),
        _row("B0AAA00003", "B0PARENT01", sales=18914.0, ads=3026.0, net=12440.0, units=25,
             ordered=25),
    ]
    parent = logic.portfolio(rows, CATALOGUE, ratings={}, decisions={},
                             today=TODAY)["parents"][0]

    assert parent["verdict"] == logic.VERDICT_SURGICAL, (
        f"a parent at {parent['net_pct']:.1%} hiding a loss-making size was judged "
        f"{parent['verdict']} — the pack-size tax would stay invisible"
    )
    # The reason must NAME the sizes, or the owner cannot act without reading the table.
    # "500g" with no space is `shipment.logic.weight_label`'s documented convention — grams
    # close up, kilos spaced ("1 kg") — because it matches how the pouches are labelled.
    assert "500g" in parent["verdict_reason"], parent["verdict_reason"]
    assert "1 size(s) lose money" in parent["verdict_reason"]


def test_an_all_positive_parent_is_not_surgical():
    """SURGICAL must mean something, so a healthy family must not carry it."""
    rows = [
        _row("B0AAA00002", "B0PARENT01", sales=28900.0, ads=8381.0, net=12600.0, units=60,
             ordered=60),
        _row("B0AAA00003", "B0PARENT01", sales=18914.0, ads=3026.0, net=12440.0, units=25,
             ordered=25),
    ]
    parent = logic.portfolio(rows, CATALOGUE, ratings={}, decisions={},
                             today=TODAY)["parents"][0]
    assert parent["verdict"] != logic.VERDICT_SURGICAL


def test_a_losing_size_with_no_volume_does_not_make_a_parent_surgical():
    """A size that sold one unit cannot condemn its family.

    Without the volume floor on the loser check, every parent with a nearly-dead pack size would
    read SURGICAL and the label would stop meaning "there is a real decision here".
    """
    rows = [
        _row("B0AAA00002", "B0PARENT01", sales=28900.0, ads=8381.0, net=12600.0, units=60,
             ordered=60),
        _row("B0AAA00001", "B0PARENT01", sales=200.0, ads=150.0, net=-60.0, units=1, ordered=1),
    ]
    parent = logic.portfolio(rows, CATALOGUE, ratings={}, decisions={},
                             today=TODAY)["parents"][0]
    assert parent["verdict"] != logic.VERDICT_SURGICAL


# ─── Arithmetic that reaches the screen ──────────────────────────────────────


def test_a_parent_is_exactly_the_sum_of_its_sizes():
    """**The invariant that stops the screen contradicting itself.**

    The expanded size rows sit directly beneath the parent row, so if the parent were computed
    from a separate parent-level Amazon query — which Amazon would happily answer — the two could
    drift and the table would visibly not add up. The same defect class as the Orders tab
    reporting 86 orders beside 87 lines.
    """
    result = logic.portfolio(_fixture(), {}, ratings={}, decisions={}, today=TODAY)
    for parent in result["parents"]:
        for field in ("sales", "ad_spend", "net", "units", "units_ordered", "units_refunded"):
            total = round(sum(size[field] for size in parent["sizes"]), 2)
            assert abs(total - parent[field]) < 0.011, (
                f"{parent['product']}: parent {field}={parent[field]} but its sizes "
                f"sum to {total}"
            )


def test_percentages_are_recomputed_from_the_sums_never_averaged():
    """Averaging the children's ratios would produce a number belonging to no product.

    Built so the two answers differ sharply: a large size at 10% TACOS beside a tiny one at 90%.
    The mean is 50%; the truth, weighted by the money, is 12%.
    """
    rows = [
        _row("B0AAA00003", "B0PARENT01", sales=100000.0, ads=10000.0, net=30000.0, units=200,
             ordered=200),
        _row("B0AAA00001", "B0PARENT01", sales=1000.0, ads=900.0, net=-500.0, units=5, ordered=5),
    ]
    parent = logic.portfolio(rows, CATALOGUE, ratings={}, decisions={},
                             today=TODAY)["parents"][0]
    assert parent["tacos"] == pytest.approx(10900 / 101000, rel=1e-6)
    assert parent["tacos"] < 0.15, (
        f"TACOS came out at {parent['tacos']:.1%} — the ratios were averaged rather than "
        "recomputed from the totals"
    )


def test_no_sales_means_no_percentage_rather_than_zero():
    """A dash on screen, never 0%.

    A product with no sales has no TACOS. Rendering that as "0%" would rank it among the most
    ad-efficient products in the portfolio, which is the opposite of true. The live data holds
    two such rows, so this is not hypothetical.
    """
    row = _row("B0AAA00001", "B0PARENT01", sales=0.0, ads=0.0, net=0.0, units=0)
    parent = logic.portfolio([row], CATALOGUE, ratings={}, decisions={},
                             today=TODAY)["parents"][0]
    assert parent["tacos"] is None, "zero sales produced a TACOS figure"
    assert parent["net_pct"] is None
    assert parent["returns_pct"] is None
    assert parent["verdict"] == logic.VERDICT_DEAD


def test_products_are_ordered_by_revenue():
    """Heaviest revenue first: the portfolio question is "where is the money"."""
    result = logic.portfolio(_fixture(), {}, ratings={}, decisions={}, today=TODAY)
    sales = [parent["sales"] for parent in result["parents"]]
    assert sales == sorted(sales, reverse=True), sales


# ─── The catalogue and rating joins ──────────────────────────────────────────


def test_an_asin_missing_from_the_catalogue_is_kept_and_named():
    """Dropped rows are how a product silently escapes review.

    It is reported in `unmatched_asins` so the screen can say which ASINs need adding to the MRP
    sheet, rather than the product simply not appearing.
    """
    row = _row("B0UNKNOWN1", "B0PARENT09", sales=5000.0, ads=1000.0, net=1500.0, units=20,
               ordered=20)
    result = logic.portfolio([row], CATALOGUE, ratings={}, decisions={}, today=TODAY)
    assert result["unmatched_asins"] == ["B0UNKNOWN1"]
    assert len(result["parents"]) == 1, "the unknown product vanished from the portfolio"
    assert result["parents"][0]["sales"] == 5000.0


def test_the_rating_is_taken_from_the_family_not_from_one_size():
    """**Amazon pools reviews across a variation family — this is measured, not assumed.**

    On the live account all sizes of one product report identical rating and count (Roasted Chana
    1 kg / 1.5 kg / 2 kg: 4.2 stars, 477 reviews), and the 261 rated ASINs carry exactly 90
    distinct (rating, count) pairs — matching the 90 parent ASINs the economics API returns.

    So a rating scraped against ANY size answers for the family, and the freshest one wins. Here
    only the 2 kg size was scraped, and the parent must still show a rating.
    """
    rows = [
        _row("B0AAA00001", "B0PARENT01", sales=10000.0, ads=2000.0, net=3000.0, units=40,
             ordered=40),
        _row("B0AAA00003", "B0PARENT01", sales=20000.0, ads=4000.0, net=7000.0, units=30,
             ordered=30),
    ]
    ratings = {"B0AAA00003": {"rating": 4.2, "rating_count": 477,
                              "scraped_at": "2026-08-18T08:17:13"}}
    parent = logic.portfolio(rows, CATALOGUE, ratings, decisions={},
                             today=TODAY)["parents"][0]
    assert parent["rating"] == 4.2
    assert parent["rating_count"] == 477


def test_the_freshest_rating_wins_when_sizes_disagree():
    """A partial re-scrape must not resurrect an older figure."""
    rows = [
        _row("B0AAA00001", "B0PARENT01", sales=10000.0, ads=2000.0, net=3000.0, units=40,
             ordered=40),
        _row("B0AAA00003", "B0PARENT01", sales=20000.0, ads=4000.0, net=7000.0, units=30,
             ordered=30),
    ]
    ratings = {
        "B0AAA00001": {"rating": 3.9, "rating_count": 100, "scraped_at": "2026-08-01T00:00:00"},
        "B0AAA00003": {"rating": 4.3, "rating_count": 180, "scraped_at": "2026-08-26T00:00:00"},
    }
    parent = logic.portfolio(rows, CATALOGUE, ratings, decisions={},
                             today=TODAY)["parents"][0]
    assert parent["rating"] == 4.3


def test_a_missing_rating_is_none_rather_than_zero():
    """Zero stars is a claim; no rating is the absence of one, and they must not look alike."""
    row = _row("B0AAA00001", "B0PARENT01", sales=10000.0, ads=2000.0, net=3000.0, units=40,
               ordered=40)
    parent = logic.portfolio([row], CATALOGUE, ratings={}, decisions={},
                             today=TODAY)["parents"][0]
    assert parent["rating"] is None
    assert parent["rating_count"] is None


def test_a_decision_is_attached_to_its_product():
    """The owner's own judgement travels with the row it belongs to."""
    row = _row("B0AAA00001", "B0PARENT01", sales=10000.0, ads=9000.0, net=-2000.0, units=40,
               ordered=40)
    decisions = {"B0PARENT01": {"decision": "kill", "note": "ads off, sell through",
                                "decided_at": "2026-08-27T10:00:00"}}
    parent = logic.portfolio([row], CATALOGUE, ratings={}, decisions=decisions,
                             today=TODAY)["parents"][0]
    assert parent["decision"] == "kill"
    assert parent["decision_note"] == "ads off, sell through"


# ─── Totals ──────────────────────────────────────────────────────────────────


def test_the_totals_are_the_sum_of_every_size_in_the_portfolio():
    """The KPI strip must agree with the table beneath it."""
    result = logic.portfolio(_fixture(), {}, ratings={}, decisions={}, today=TODAY)
    totals = result["totals"]
    for field in ("sales", "ad_spend", "net", "units"):
        expected = round(sum(p[field] for p in result["parents"]), 2)
        assert abs(totals[field] - expected) < 0.011, field
    assert totals["parents"] == len(result["parents"])
    assert sum(totals["verdicts"].values()) == len(result["parents"]), (
        "the verdict counts do not add up to the number of products, so the filter chips "
        "would not reach every row"
    )


def test_fees_are_summed_by_type_across_sizes():
    """The fee breakdown must survive the rollup, since it explains where the margin went."""
    rows = [
        _row("B0AAA00001", "B0PARENT01", sales=10000.0, net=3000.0, units=40, ordered=40,
             fees={"ReferralFee": 500.0, "FbaFulfilmentFee": 800.0}),
        _row("B0AAA00003", "B0PARENT01", sales=20000.0, net=7000.0, units=30, ordered=30,
             fees={"ReferralFee": 900.0, "WeightBasedFee": 300.0}),
    ]
    parent = logic.portfolio(rows, CATALOGUE, ratings={}, decisions={},
                             today=TODAY)["parents"][0]
    assert parent["fees"]["ReferralFee"] == 1400.0
    assert parent["fees"]["FbaFulfilmentFee"] == 800.0
    assert parent["fees"]["WeightBasedFee"] == 300.0
    assert parent["fees_total"] == 2500.0


# ─── ACOS: the thing SP-API cannot answer ────────────────────────────────────
#
# TACOS = spend / TOTAL sales      -> how ad-dependent is this product?
# ACOS  = spend / ATTRIBUTED sales -> do the ads pay for themselves?
#
# Measured account-wide: TACOS 33.1% against a true ACOS of 89.9%, so Rs 1 of ads returns Rs 1.11.
# They are different claims about the same money and both stay on screen.


def _ads(cost, attributed, *, clicks=0, impressions=0, purchases=0):
    return {"cost": cost, "attributed_sales": attributed,
            "clicks": clicks, "impressions": impressions, "purchases": purchases}


def test_acos_is_spend_over_attributed_sales_not_over_total_sales():
    """The two ratios must not be computed from the same denominator.

    Built so they differ sharply: Rs 10,000 of ads against Rs 100,000 total sales but only
    Rs 8,000 attributed. TACOS is 10%; ACOS is 125%. A product can look cheap to advertise and
    still be losing money on every ad — which is exactly the case TACOS alone cannot see.
    """
    row = _row("B0AAA00002", "B0PARENT01", sales=100000.0, ads=10000.0, net=30000.0,
               units=200, ordered=200)
    result = logic.portfolio([row], CATALOGUE, ratings={}, decisions={}, today=TODAY,
                             ads_by_asin={"B0AAA00002": _ads(10000.0, 8000.0)})
    parent = result["parents"][0]
    assert parent["tacos"] == pytest.approx(0.10)
    assert parent["acos"] == pytest.approx(1.25)


def test_an_unadvertised_product_has_no_acos_rather_than_zero():
    """**None, never 0.0** — the same trap `_ratio` guards for TACOS.

    A product with no ad spend has no ACOS. Rendering it as 0% would place it at the top of an
    ACOS sort, i.e. the most efficient advertising in the portfolio, which is the opposite of
    true: it is not advertised at all.
    """
    row = _row("B0AAA00001", "B0PARENT01", sales=10000.0, ads=0.0, net=3000.0,
               units=40, ordered=40)
    parent = logic.portfolio([row], CATALOGUE, ratings={}, decisions={}, today=TODAY,
                             ads_by_asin={})["parents"][0]
    assert parent["acos"] is None
    assert parent["acos_infinite"] is False


def test_spend_with_no_attributed_sales_is_flagged_rather_than_divided():
    """A third state, and it needs its own flag because a ratio cannot express it.

    Measured on the live account: Rs 55,217 across 591 rows produced ZERO attributed sales. That
    is neither "no ACOS" (there IS spend) nor a number (division by zero), so it travels as
    `acos_infinite` and the screen says "no sales" rather than inventing a large percentage.
    """
    row = _row("B0AAA00001", "B0PARENT01", sales=10000.0, ads=1444.0, net=3000.0,
               units=40, ordered=40)
    parent = logic.portfolio([row], CATALOGUE, ratings={}, decisions={}, today=TODAY,
                             ads_by_asin={"B0AAA00001": _ads(1444.0, 0.0)})["parents"][0]
    assert parent["acos"] is None, "a zero denominator produced a ratio"
    assert parent["acos_infinite"] is True


def test_acos_uses_the_ads_apis_own_cost_not_the_economics_ad_spend():
    """Two APIs, two attribution windows — mixing them would be a ratio of different things.

    They reconcile to 0.2% account-wide (Rs 16,32,415 against Rs 16,35,983), but per product the
    gap is larger, and dividing one source's cost by another's sales is not a meaningful number.

    **Checked at BOTH grains, and a mutation is why.** ACOS is computed twice — in `size_row` for
    a pack size and in `_sum_sizes` for its parent — so breaking one leaves the other correct. A
    mutation to `size_row` alone survived a version of this test that only looked at the parent,
    while the SKU view (the grain that was actually asked for) showed 56% instead of 50%.
    """
    row = _row("B0AAA00001", "B0PARENT01", sales=50000.0, ads=9000.0, net=10000.0,
               units=100, ordered=100)
    result = logic.portfolio([row], CATALOGUE, ratings={}, decisions={}, today=TODAY,
                             ads_by_asin={"B0AAA00001": _ads(8000.0, 16000.0)})
    parent = result["parents"][0]
    size = parent["sizes"][0]
    sku = result["skus"][0]

    # 8000 / 16000, from the ads API — NOT 9000 (the economics ad charge) / 16000 = 0.5625.
    for label, row_out in (("parent", parent), ("size", size), ("sku row", sku)):
        assert row_out["acos"] == pytest.approx(0.50), (
            f"{label} ACOS is {row_out['acos']:.4f}; 0.5625 means it divided the ECONOMICS ad "
            "charge by attributed sales instead of the Advertising API's own cost"
        )
    assert parent["ads_cost"] == 8000.0
    assert parent["ad_spend"] == 9000.0, "the economics ad charge was overwritten"


# ─── AD DEPENDENT: a verdict only ACOS makes expressible ──────────────────────


def test_a_profitable_product_whose_ads_lose_money_is_ad_dependent():
    """**The verdict the Advertising API added.**

    Measured live: Roasted Chana earns +31.9% net at 104% ACOS, and Moringa Sattu +17.4% at 182%.
    Under TACOS alone both read as MONITOR or BEST BET, because TACOS' denominator includes the
    organic sales that are carrying them. The action differs from KILL: cut the spend, keep the
    product.
    """
    row = _row("B0AAA00002", "B0PARENT01", sales=100000.0, ads=25000.0, net=30000.0,
               units=300, ordered=300)
    parent = logic.portfolio([row], CATALOGUE, ratings={}, decisions={}, today=TODAY,
                             ads_by_asin={"B0AAA00002": _ads(25000.0, 20000.0)})["parents"][0]
    assert parent["verdict"] == logic.VERDICT_AD_DEPENDENT, (
        f"net {parent['net_pct']:.1%} at ACOS {parent['acos']:.0%} was judged {parent['verdict']}"
    )
    assert "cut the spend" in parent["verdict_reason"]


def test_spend_with_no_attributed_sales_also_makes_a_product_ad_dependent():
    """Worse than a bad ratio, and it must not fall through to MONITOR for lack of one."""
    row = _row("B0AAA00002", "B0PARENT01", sales=100000.0, ads=5000.0, net=30000.0,
               units=300, ordered=300)
    parent = logic.portfolio([row], CATALOGUE, ratings={}, decisions={}, today=TODAY,
                             ads_by_asin={"B0AAA00002": _ads(5000.0, 0.0)})["parents"][0]
    assert parent["verdict"] == logic.VERDICT_AD_DEPENDENT
    assert "no attributed sales" in parent["verdict_reason"]


def test_a_loss_making_product_is_still_KILL_rather_than_ad_dependent():
    """Rule ORDER: KILL outranks AD DEPENDENT, because the product itself is the problem.

    AD DEPENDENT means "the product earns money but the ads do not" — it cannot apply to a
    product that is losing money, where cutting ads alone would not fix it.
    """
    row = _row("B0AAA00001", "B0PARENT01", sales=32898.0, ads=25660.0, net=-18686.0,
               units=204, ordered=208, refunded=4)
    parent = logic.portfolio([row], CATALOGUE, ratings={}, decisions={}, today=TODAY,
                             ads_by_asin={"B0AAA00001": _ads(25660.0, 5000.0)})["parents"][0]
    assert parent["verdict"] == logic.VERDICT_KILL


def test_an_efficiently_advertised_product_is_still_a_best_bet():
    """AD DEPENDENT must not swallow the healthy case, or the verdict means nothing."""
    row = _row("B0AAA00003", "B0PARENT01", sales=100000.0, ads=25000.0, net=40000.0,
               units=400, ordered=400)
    parent = logic.portfolio([row], CATALOGUE,
                             ratings={"B0AAA00003": {"rating": 4.4, "rating_count": 300}},
                             decisions={}, today=TODAY,
                             ads_by_asin={"B0AAA00003": _ads(25000.0, 120000.0)})["parents"][0]
    assert parent["verdict"] == logic.VERDICT_BEST_BET
    assert "ACOS 21%" in parent["verdict_reason"], parent["verdict_reason"]


# ─── Editable thresholds ─────────────────────────────────────────────────────


def test_a_raised_acos_threshold_moves_a_product_out_of_ad_dependent():
    """The thresholds are a PARAMETER, not module constants.

    Verified live: dropping break-even ACOS from 100% to 60% moved AD DEPENDENT from 6 products to
    18. A mutation that ignores the passed dict and reads the constants must fail here.
    """
    row = _row("B0AAA00002", "B0PARENT01", sales=100000.0, ads=25000.0, net=30000.0,
               units=300, ordered=300)
    ads_rows = {"B0AAA00002": _ads(25000.0, 20000.0)}          # ACOS 125%

    strict = logic.portfolio([row], CATALOGUE, ratings={}, decisions={}, today=TODAY,
                             ads_by_asin=ads_rows)["parents"][0]
    assert strict["verdict"] == logic.VERDICT_AD_DEPENDENT

    lenient = logic.portfolio([row], CATALOGUE, ratings={}, decisions={}, today=TODAY,
                              ads_by_asin=ads_rows,
                              thresholds={"break_even_acos": 1.5})["parents"][0]
    assert lenient["verdict"] != logic.VERDICT_AD_DEPENDENT, (
        "raising the break-even threshold above this product's ACOS did not change its verdict, "
        "so the saved thresholds are being ignored"
    )


def test_a_lowered_kill_tacos_threshold_creates_more_kills():
    """A product between the two thresholds must move, or the setting does nothing."""
    row = _row("B0AAA00001", "B0PARENT01", sales=40000.0, ads=18000.0, net=-2000.0,
               units=150, ordered=150)          # TACOS 45%: between 40% and 50%

    default = logic.portfolio([row], CATALOGUE, ratings={}, decisions={}, today=TODAY)["parents"][0]
    assert default["verdict"] == logic.VERDICT_MONITOR, (
        "at the default 50% threshold a 45%-TACOS loss should not be a KILL"
    )

    strict = logic.portfolio([row], CATALOGUE, ratings={}, decisions={}, today=TODAY,
                             thresholds={"kill_tacos": 0.40})["parents"][0]
    assert strict["verdict"] == logic.VERDICT_KILL


def test_a_partial_threshold_dict_falls_back_for_everything_absent():
    """A half-saved settings row must not take the dashboard down."""
    merged = logic.thresholds_or_default({"kill_tacos": 0.4})
    assert merged["kill_tacos"] == 0.4
    assert merged["good_net"] == logic.DEFAULT_THRESHOLDS["good_net"]
    assert set(merged) == set(logic.DEFAULT_THRESHOLDS)


def test_an_unusable_threshold_value_is_ignored_rather_than_crashing():
    """Text where a number belongs falls back instead of raising mid-render."""
    merged = logic.thresholds_or_default({"kill_tacos": "not a number", "good_net": None})
    assert merged["kill_tacos"] == logic.DEFAULT_THRESHOLDS["kill_tacos"]
    assert merged["good_net"] == logic.DEFAULT_THRESHOLDS["good_net"]


def test_every_verdict_has_help_text_that_substitutes_the_live_thresholds():
    """The panel explains each rule, and the explanation must follow an edited number.

    Help text naming 50% while the rule fires at 40% would be worse than no help at all.
    """
    for verdict in logic.VERDICT_ORDER:
        assert verdict in logic.VERDICT_HELP, f"{verdict} has no explanation"
    rendered = logic.VERDICT_HELP[logic.VERDICT_KILL].format(
        **logic.thresholds_or_default({"kill_tacos": 0.40})
    )
    assert "40%" in rendered, rendered


# ─── The SKU view and the merchant/FBA split ─────────────────────────────────


def test_the_sku_view_is_one_row_per_pack_size_carrying_its_parents_name():
    """Asked for as a view of "the easy ship and fba sku's combined data not separate".

    The rows ARE the size dicts already under each parent, so a SKU row and its product can never
    disagree about a number. Each already combines both channels, because that is what Amazon's
    CHILD_ASIN aggregation does.
    """
    rows = [
        _row("B0AAA00001", "B0PARENT01", sales=21400.0, ads=17334.0, net=-6420.0, units=80,
             ordered=80),
        _row("B0AAA00002", "B0PARENT01", sales=28900.0, ads=8381.0, net=12600.0, units=60,
             ordered=60),
    ]
    result = logic.portfolio(rows, CATALOGUE, ratings={}, decisions={}, today=TODAY)

    assert len(result["skus"]) == 2
    assert result["totals"]["skus"] == 2
    for sku in result["skus"]:
        assert sku["product"], "a SKU row has no product name"
        assert sku["parent_product"] == "Test Sattu"
        assert sku["verdict"], "a SKU row has no verdict of its own"
    # Sorted by revenue, like the products.
    assert result["skus"][0]["sales"] >= result["skus"][1]["sales"]


def test_sku_verdicts_are_counted_separately_from_product_verdicts():
    """"9 KILL products" and "24 KILL SKUs" are both true, and the chips must match the grain.

    Measured live: 9 KILL products against 24 KILL sizes. A single count would contradict whichever
    table was on screen.
    """
    rows = [
        _row("B0AAA00001", "B0PARENT01", sales=21400.0, ads=17334.0, net=-6420.0, units=80,
             ordered=80),
        _row("B0AAA00002", "B0PARENT01", sales=28900.0, ads=8381.0, net=12600.0, units=60,
             ordered=60),
    ]
    totals = logic.portfolio(rows, CATALOGUE, ratings={}, decisions={},
                             today=TODAY)["totals"]
    assert "verdicts" in totals and "sku_verdicts" in totals
    assert sum(totals["sku_verdicts"].values()) == totals["skus"]
    assert sum(totals["verdicts"].values()) == totals["parents"]


def test_a_sku_row_carries_no_rating_driven_verdict():
    """Ratings are pooled per family, so they cannot discriminate between sizes.

    A size row shows its PARENT's stars for context, but its verdict is economics-only — letting
    a family rating decide a per-size verdict would imply a precision the review data lacks.
    """
    rows = [
        _row("B0AAA00003", "B0PARENT01", sales=100000.0, ads=25000.0, net=40000.0, units=400,
             ordered=400),
    ]
    result = logic.portfolio(rows, CATALOGUE,
                             ratings={"B0AAA00003": {"rating": 3.2, "rating_count": 10}},
                             decisions={}, today=TODAY)
    parent, sku = result["parents"][0], result["skus"][0]
    # The parent is SCALE because of the poor rating...
    assert parent["verdict"] == logic.VERDICT_SCALE
    # ...while the size is judged on economics alone.
    assert sku["verdict"] == logic.VERDICT_BEST_BET
    assert sku["rating"] == 3.2, "the size should still SHOW the family rating for context"


def test_the_channel_split_sums_to_the_combined_row():
    """Merchant + FBA must equal what the size row shows, or the expansion contradicts it.

    Measured across the account: merchant 16,68,051 + FBA 32,81,373 = 49,49,424, matching the
    ASIN-level total to the rupee.
    """
    sku_rows = [
        {"childAsin": "B0AAA00001", "msku": "0.5 fc np",
         "sales": {"orderedProductSales": {"amount": 18448.0}, "netUnitsSold": 105},
         "netProceeds": {"total": {"amount": 5000.0}}},
        {"childAsin": "B0AAA00001", "msku": "0.5 fc np FBA",
         "sales": {"orderedProductSales": {"amount": 14658.0}, "netUnitsSold": 95},
         "netProceeds": {"total": {"amount": 3000.0}}},
    ]
    split = logic.channel_split(sku_rows)
    assert set(split["B0AAA00001"]) == {"merchant", "fba"}
    assert split["B0AAA00001"]["merchant"]["sales"] == 18448.0
    assert split["B0AAA00001"]["fba"]["sales"] == 14658.0
    assert split["B0AAA00001"]["merchant"]["units"] == 105
    assert split["B0AAA00001"]["fba"]["units"] == 95


def test_the_fba_suffix_is_what_marks_the_channel():
    """Measured across all 453 MSKU rows and 213 advertised SKUs: the suffix is the only marker.

    A convention of THIS account rather than an Amazon rule, which is why the check lives in one
    function — `_channel_of` is the single place to fix if the naming ever changes.
    """
    assert logic._channel_of("0.5 fc np FBA") == logic.CHANNEL_FBA
    assert logic._channel_of("0.5 fc np") == logic.CHANNEL_MERCHANT
    assert logic._channel_of("2kg kc FBA") == logic.CHANNEL_FBA
    # Case-insensitive, since Amazon's casing is not guaranteed.
    assert logic._channel_of("2kg kc fba") == logic.CHANNEL_FBA
    # And a product whose NAME contains fba is not misfiled — the token must stand alone.
    assert logic._channel_of("fbagel 1kg") == logic.CHANNEL_MERCHANT


def test_the_channel_split_carries_per_channel_acos():
    """The reason the split is worth showing at all.

    Measured: the merchant SKU of B0DCCL1531 spent Rs 1,444 on ads for ZERO attributed sales while
    its FBA twin returned 36% ACOS. One combined number hides a fulfilment decision.
    """
    sku_rows = [
        {"childAsin": "B0AAA00001", "msku": "2kg kc",
         "sales": {"orderedProductSales": {"amount": 10000.0}, "netUnitsSold": 50},
         "netProceeds": {"total": {"amount": 2000.0}}},
        {"childAsin": "B0AAA00001", "msku": "2kg kc FBA",
         "sales": {"orderedProductSales": {"amount": 20000.0}, "netUnitsSold": 90},
         "netProceeds": {"total": {"amount": 6000.0}}},
    ]
    ads_by_sku = {
        ("B0AAA00001", "2kg kc"): _ads(1444.0, 0.0),
        ("B0AAA00001", "2kg kc FBA"): _ads(5176.0, 14254.0),
    }
    split = logic.channel_split(sku_rows, ads_by_sku)["B0AAA00001"]
    assert split["merchant"]["acos_infinite"] is True
    assert split["merchant"]["acos"] is None
    assert split["fba"]["acos"] == pytest.approx(5176.0 / 14254.0)


def test_a_sku_row_with_no_msku_is_skipped_rather_than_misfiled():
    """Amazon's schema allows a null msku when a row spans several FNSKUs.

    Such a row belongs to no single channel, so filing it under one would misattribute real money.
    """
    split = logic.channel_split([
        {"childAsin": "B0AAA00001", "msku": None,
         "sales": {"orderedProductSales": {"amount": 9999.0}, "netUnitsSold": 50}},
    ])
    assert split == {}
