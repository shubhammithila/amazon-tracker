"""The stock CSV parser: a file it cannot read must be REFUSED, not read as zero.

**This exists because of a real over-production near-miss.** On 07 Sep 2026 the owner uploaded a
stock report whose quantity columns were not the ones this parser looks for. `asin` and `sku` matched,
so merchant SKUs populated and the file looked accepted — but `existing_cols` came out empty,
`sum([])` is `0`, and every ASIN was recorded as holding no FBA stock.

`deficit = projection - fba_stock`, so every deficit became the full projection. Measured on
production:

    plan 1 (11 Aug)  sum(fba_stock) =  9,532
    plan 2 (20 Aug)  sum(fba_stock) =  6,390
    plan 3 (30 Aug)  sum(fba_stock) = 10,659
    plan 4 (07 Sep)  sum(fba_stock) =      0   <- the bad upload

The plan asked for ~18,955 units against a real need of roughly 8,000. **Two `POST
/shipment/generate` calls returned 200 OK and the log carried no warning**, so nothing anywhere said
the file had not been understood.

The asymmetry that allowed it is worth keeping in mind: `parse_sku_map`, ten lines below in the same
module, already logs a warning when its columns are missing. The stock parser had no equivalent.
"""
from __future__ import annotations

import pytest

from app.routers.shipment import parse_sku_map, parse_stock_csv

pytestmark = pytest.mark.regression


#: The real Amazon FBA inventory report's quantity columns, as of Sep 2026.
GOOD = (
    b"sku,asin,afn-fulfillable-quantity,afn-reserved-quantity,afn-inbound-shipped-quantity\n"
    b"abc_sattu500g FBA,B0H55V3FJ3,120,10,50\n"
    b"abc_sattu1kg FBA,B0H561RM7X,0,0,0\n"
)


def test_the_real_report_still_parses():
    """The regression guard: the guard added below must not break the working case."""
    assert parse_stock_csv(GOOD) == {"B0H55V3FJ3": 180, "B0H561RM7X": 0}


def test_a_file_with_no_recognisable_quantity_column_is_refused():
    """**The bug. This is the file that cost ~10,000 units of over-production.**

    It has valid `asin` and `sku` — so it IS an Amazon export, and the merchant SKUs populated
    normally, which is what made it look accepted. What it does not have is any column this parser
    knows how to read a quantity from.

    Refused rather than read as zero, for the same reason a missing `asin` is refused: a stock report
    the app cannot understand is not a stock report holding no stock.
    """
    wrong_report = (
        b"sku,asin,quantity,fulfillable-quantity,price\n"
        b"abc_sattu500g FBA,B0H55V3FJ3,120,130,499\n"
    )
    with pytest.raises(ValueError) as caught:
        parse_stock_csv(wrong_report)

    message = str(caught.value)
    # The refusal has to be ACTIONABLE. "Invalid stock report" sends the owner back to Seller
    # Central with no idea which of the ~six inventory reports to pick.
    assert "afn-fulfillable-quantity" in message, "the message must name a column it looked for"
    assert "quantity" in message, "the message must name what the file actually had"


def test_the_refusal_names_the_report_to_download():
    """A refusal the owner can act on without asking anybody.

    There are several inventory reports in Seller Central and their columns differ; naming the right
    one is the difference between a 30-second fix and a support conversation.
    """
    with pytest.raises(ValueError) as caught:
        parse_stock_csv(b"sku,asin,price\nS,B0H55V3FJ3,499\n")
    assert "FBA" in str(caught.value), "the message must say which report to download"


def test_a_report_with_only_one_of_the_eight_columns_is_accepted():
    """**Partial is fine and must stay fine.** Amazon's report varies by account and by region;
    several of the eight columns are frequently absent, and the parser sums whichever exist.

    Asserted so the guard cannot be tightened into "all eight or nothing", which would refuse the
    real report most accounts get. `test_shipment_catalogue.py` alone builds fixtures with just
    `afn-fulfillable-quantity`.
    """
    assert parse_stock_csv(
        b"asin,sku,afn-fulfillable-quantity\nB0H55V3FJ3,kw 500g FBA,7\n"
    ) == {"B0H55V3FJ3": 7}


def test_a_small_genuinely_empty_warehouse_is_still_zero_not_an_error():
    """**Zero stock is a real answer for a SMALL account and must not be refused.**

    A new seller, or one who has sold out of everything, honestly holds nothing. Below
    `ALL_ZERO_STOCK_THRESHOLD` products an all-zero report is therefore accepted as fact.

    This test and the one below are the two sides of a genuine ambiguity: "every total is zero" can
    mean an empty warehouse OR a file whose values could not be read, and nothing in the file itself
    distinguishes them. Scale is the only available signal.
    """
    assert parse_stock_csv(
        b"asin,sku,afn-fulfillable-quantity,afn-inbound-shipped-quantity\n"
        b"B0H55V3FJ3,a 500g FBA,0,0\nB0H561RM7X,b 1kg FBA,0,0\n"
    ) == {"B0H55V3FJ3": 0, "B0H561RM7X": 0}


def test_a_large_report_that_is_entirely_zero_is_refused_as_unreadable():
    """**The 07 Sep bug's second cause, and the one that got past the first fix.**

    The column was present and matched — so the missing-column guard passed and
    `POST /shipment/generate` returned 200 — but `_clean_number` turns anything unparseable into
    `0.0`: a blank, a dash, "N/A", even a quoted `"474"`. Every one of 108 products came out zero and
    the plan asked for 18,955 units.

    A report listing dozens of active products where every single one holds nothing is a parse
    failure, not a warehouse.
    """
    header = b"asin,sku,afn-fulfillable-quantity,afn-inbound-shipped-quantity\n"
    # 12 products, all unreadable values — above the threshold of 10.
    rows = b"".join(
        f"B0PRODUCT{i:02d},sku {i} FBA,N/A,N/A\n".encode() for i in range(12)
    )
    with pytest.raises(ValueError) as caught:
        parse_stock_csv(header + rows)

    message = str(caught.value)
    assert "zero stock" in message, "the message must say what it found"
    # Actionable: it must show the values it read, so "N/A" or blank is visible at a glance.
    assert "afn-fulfillable-quantity" in message, "must name the column(s) it did match"
    assert "Excel" in message, "the likeliest cause is Excel rewriting the file — say so"


def test_the_threshold_is_wide_enough_that_neither_case_is_marginal():
    """The threshold is a judgement call, so it is pinned with its reasoning.

    Measured on this account: 108 active products, previous plan held 10,659 units. 10 sits well below
    that and well above the handful a genuinely new seller would list, so neither reading is decided
    by one product either way.
    """
    from app.routers.shipment import ALL_ZERO_STOCK_THRESHOLD

    assert 5 <= ALL_ZERO_STOCK_THRESHOLD <= 30, (
        "below ~5 a genuinely new seller gets refused; above ~30 a real parse failure on a "
        "medium account slips through"
    )


def test_a_missing_asin_column_is_still_refused():
    """The pre-existing guard, pinned so the new one cannot be written in a way that loses it."""
    with pytest.raises(ValueError, match="asin"):
        parse_stock_csv(b"sku,afn-fulfillable-quantity\nS,10\n")


async def test_generate_refuses_the_bad_report_rather_than_building_a_zero_plan(
    auth_client, db, monkeypatch
):
    """**End to end, because the parser was never the thing the owner saw.**

    `POST /shipment/generate` returned 200 twice with this file. A unit test on the parser would not
    have caught that, because the route wraps it in `try/except` and only turns an exception into a
    400 — so the guard is only useful if the exception actually reaches there.
    """
    from app.shipment import catalogue

    async def fake_catalogue():
        return ([{"asin": "B0H55V3FJ3", "name": "ABC Sattu", "weight": 0.5,
                  "brand": "MF", "active": True}], None, "test")

    monkeypatch.setattr(catalogue, "load_catalogue", fake_catalogue)

    response = await auth_client.post("/shipment/generate", files={
        # `(Child) ASIN` is what the Business Report actually calls it — the sales parser is
        # validated first, so a wrong header here would fail before the stock file is even read.
        "sales_csv": ("s.csv", b"(Child) ASIN,Units Ordered\nB0H55V3FJ3,87\n", "text/csv"),
        # asin and sku present, quantity columns absent — the 07 Sep file.
        "stock_csv": ("k.csv", b"sku,asin,quantity,price\nS,B0H55V3FJ3,120,499\n", "text/csv"),
    })

    assert response.status_code == 400, (
        f"generate returned {response.status_code} for an unreadable stock report — this is exactly "
        f"how a plan of 18,955 units got built against a real need of ~8,000"
    )
    assert "Stock CSV error" in response.json().get("error", "")


# ─── The Flex/FBA collision: the real 07 Sep cause ───────────────────────────


def test_a_flex_sku_row_does_not_overwrite_the_fba_row():
    """**The bug that made every product read zero, found by the owner.**

    One ASIN appears on several SKU rows and only the FBA one holds FBA stock. Measured on the real
    file:

        0.5kg cs 1 FBA   B0CWGXYLT6   474      <- the FBA row, the real stock
        0.5kg cs 1 flex  B0CWGXYLT6     0      <- the Flex row, holds no FBA stock

    The loop did `asin_stock[asin] = total`, so the LAST row won and 474 became 0.
    """
    csv = (
        b"sku,asin,afn-fulfillable-quantity,afn-reserved-quantity\n"
        b"0.5kg cs 1 FBA,B0CWGXYLT6,474,0\n"
        b"0.5kg cs 1 flex,B0CWGXYLT6,0,0\n"
    )
    assert parse_stock_csv(csv) == {"B0CWGXYLT6": 474}


def test_the_result_does_not_depend_on_row_order():
    """**Order-dependence is what made this intermittent**, and why plans 1-3 were fine.

    The same file read 0 or 474 purely according to which row came last. Both orders must now give
    the same answer, or the bug is only half fixed.
    """
    fba = b"0.5kg cs 1 FBA,B0CWGXYLT6,474,0\n"
    flex = b"0.5kg cs 1 flex,B0CWGXYLT6,0,0\n"
    header = b"sku,asin,afn-fulfillable-quantity,afn-reserved-quantity\n"
    assert parse_stock_csv(header + fba + flex) == parse_stock_csv(header + flex + fba)


def test_several_fba_skus_for_one_asin_are_SUMMED_not_replaced():
    """Summed rather than "take the FBA row", because an ASIN can have more than one FBA SKU.

    Picking one would under-state stock and send the warehouse making units that already exist — the
    same reasoning behind `ads.logic.aggregate` collapsing a split report rather than choosing a row.
    """
    csv = (
        b"sku,asin,afn-fulfillable-quantity\n"
        b"0.5kg cs 1 FBA,B0CWGXYLT6,300\n"
        b"0.5kg cs 2 FBA,B0CWGXYLT6,174\n"
        b"0.5kg cs 1 flex,B0CWGXYLT6,0\n"
    )
    assert parse_stock_csv(csv) == {"B0CWGXYLT6": 474}


def test_an_asin_with_only_a_flex_sku_holds_no_fba_stock():
    """Correctly absent, not zero-by-accident.

    A product sold only on Easy Ship genuinely has no FBA stock, so it must not appear in the map at
    all — `stock.get(asin, 0)` then gives 0 for the right reason. This is the case that makes the
    all-zero guard's threshold necessary rather than a flat refusal.
    """
    csv = b"sku,asin,afn-fulfillable-quantity\n0.25 fc ch,B0CY84RYRG,0\n"
    assert parse_stock_csv(csv) == {}


def test_the_sku_map_prefers_the_fba_sku_over_the_flex_one():
    """**Amazon's shipment upload keys on the merchant SKU**, so the wrong one is a rejected line.

    `setdefault` alone means the FIRST row wins, which on this account can be the Flex SKU. It picked
    the right one on the 07 Sep file by luck of ordering — the same accident that made the stock read
    zero.
    """
    flex_first = (
        b"sku,asin,afn-fulfillable-quantity\n"
        b"0.5kg cs 1 flex,B0CWGXYLT6,0\n"
        b"0.5kg cs 1 FBA,B0CWGXYLT6,474\n"
    )
    assert parse_sku_map(flex_first) == {"B0CWGXYLT6": "0.5kg cs 1 FBA"}


def test_an_easy_ship_only_asin_keeps_its_own_sku_rather_than_blank():
    """A blank SKU is a line Amazon rejects, so a non-FBA SKU is better than nothing.

    Used only when the ASIN has no FBA SKU at all — a fallback, not a competitor for ordering.
    """
    assert parse_sku_map(
        b"sku,asin,afn-fulfillable-quantity\n0.25 fc ch,B0CY84RYRG,0\n"
    ) == {"B0CY84RYRG": "0.25 fc ch"}


def test_the_owners_real_column_layout_parses():
    """Columns K, M and P-U of the real report, named by the owner as the ones to sum.

    Verified against the actual file's header: those eight are exactly the eight this parser already
    looked for, which is why the missing-column guard passed and the zeros came from the Flex rows
    instead.
    """
    header = (
        b"sku,fnsku,asin,product-name,condition,your-price,mfn-listing-exists,"
        b"mfn-fulfillable-quantity,afn-listing-exists,afn-warehouse-quantity,"
        b"afn-fulfillable-quantity,afn-unsellable-quantity,afn-reserved-quantity,"
        b"afn-total-quantity,per-unit-volume,afn-inbound-working-quantity,"
        b"afn-inbound-shipped-quantity,afn-inbound-receiving-quantity,"
        b"afn-researching-quantity,afn-reserved-future-supply,afn-future-supply-buyable,store\n"
    )
    #                                             K=100  M=7   P=0 Q=170 R=0 S=0 T=0 U=0
    row = (
        b"0.5kg cs 1 FBA,X002BZGZ,B0CWGXYLT6,MITHILA,New,179,No,0,Yes,57,"
        b"100,0,7,227,1378.97,0,170,0,0,0,0,store\n"
    )
    # afn-fulfillable(100) + afn-reserved(7) + inbound-shipped(170) = 277.
    # afn-warehouse-quantity(57) and afn-total-quantity(227) are deliberately NOT summed: they
    # double-count the others, which is why the eight-column list excludes them.
    assert parse_stock_csv(header + row) == {"B0CWGXYLT6": 277}
