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

from app.routers.shipment import parse_stock_csv

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
        b"asin,sku,afn-fulfillable-quantity\nB0H55V3FJ3,S,7\n"
    ) == {"B0H55V3FJ3": 7}


def test_a_genuinely_empty_warehouse_is_still_zero_not_an_error():
    """**Zero stock is a real answer and must not be confused with an unreadable file.**

    A new account, or a seller who has sold out, genuinely holds nothing. The distinction this whole
    file is about is between "the column says 0" and "there is no column" — so a recognisable report
    full of zeros parses fine.
    """
    assert parse_stock_csv(
        b"asin,sku,afn-fulfillable-quantity,afn-inbound-shipped-quantity\n"
        b"B0H55V3FJ3,S,0,0\nB0H561RM7X,T,0,0\n"
    ) == {"B0H55V3FJ3": 0, "B0H561RM7X": 0}


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
