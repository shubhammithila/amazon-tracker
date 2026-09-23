"""Weight SOLD: units × the pack size from the MRP sheet's column B.

Asked for as *"total weight sold also should be column for parent and child sku's both. it can be
done by multiplying the number of units to the weight of the item/sku… weight is there in MRP sheet
column B."*

Two properties carry the whole feature, and each has a named precedent in this codebase:

* **A parent is the SUM of its sizes**, never its own multiplication — a parent holds 0.5 kg, 1 kg
  and 2 kg packs and has no single pack weight, so there is nothing to multiply by. Same
  construction that already makes `sales` and `net` agree between a parent row and the rows
  beneath it.
* **An unknown pack size is EXCLUDED and COUNTED, never treated as 0 kg.** `shipment_weight`'s
  docstring states why: *"a line silently contributing nothing is how a 130 kg shipment reports
  90"*. Measured on the live sheet, 0 of 273 entries have an unusable weight — so **no real row
  exercises this path**, which is exactly why the fixtures below construct one.
"""
import pytest

from app.portfolio import logic
from app.shipment.logic import line_weight

pytestmark = pytest.mark.regression

#: Two sizes of one parent with real weights, plus one the sheet knows nothing about.
CATALOGUE = {
    "B0DK1LJ3N3": {"name": "Chana Sattu", "weight": 1.0, "brand": "Mithila Foods"},
    "B0CWGXYLT6": {"name": "Chana Sattu", "weight": 0.5, "brand": "Mithila Foods"},
    "B0NOWEIGHT": {"name": "Chana Sattu", "brand": "Mithila Foods"},   # blank Net Weight cell
    "B0SMALLPCK": {"name": "Moringa Powder", "weight": 0.15, "brand": "Howrah Foods"},
}


def _row(child, parent, *, units, sales=1000.0):
    return {
        "parentAsin": parent,
        "childAsin": child,
        "sales": {
            "orderedProductSales": {"amount": sales, "currencyCode": "INR"},
            "refundedProductSales": {"amount": 0.0, "currencyCode": "INR"},
            "unitsOrdered": units,
            "unitsRefunded": 0,
            "netUnitsSold": units,
        },
        "fees": [],
        "ads": [],
        "netProceeds": {"total": {"amount": sales * 0.3}},
    }


def _parent(rows, catalogue=None):
    return logic.portfolio(rows, catalogue or CATALOGUE, ratings={}, decisions={})["parents"][0]


# ─── The multiplication, at every grain ──────────────────────────────────────


def test_weight_sold_is_units_times_the_pack_size_at_ALL_THREE_grains():
    """Parent, size and SKU row must agree, and the reason is a bug this tab already had.

    ACOS is computed in two places — `size_row` and `_sum_sizes` — and a mutation to one alone
    passed all 42 logic tests while the SKU view showed 56% where the parent showed 50%: the parent
    figure is recomputed and masked it. Weight has the same shape, so it is asserted at every grain
    rather than at whichever one the fixture happens to expose.
    """
    rows = [_row("B0DK1LJ3N3", "P1", units=774), _row("B0CWGXYLT6", "P1", units=359)]
    result = logic.portfolio(rows, CATALOGUE, ratings={}, decisions={})
    parent = result["parents"][0]
    by_asin = {s["asin"]: s for s in parent["sizes"]}
    skus = {s["asin"]: s for s in result["skus"]}

    assert by_asin["B0DK1LJ3N3"]["weight_kg"] == 774.0        # 774 × 1.0 kg
    assert by_asin["B0CWGXYLT6"]["weight_kg"] == 179.5        # 359 × 0.5 kg
    assert parent["weight_kg"] == 953.5, "the parent is not the sum of its sizes"
    # The SKU grain is the same rows flattened, so it must carry the same figures.
    assert skus["B0DK1LJ3N3"]["weight_kg"] == 774.0
    assert skus["B0CWGXYLT6"]["weight_kg"] == 179.5
    # ...and the account total equals the parent, since there is one parent.
    assert result["totals"]["weight_kg"] == 953.5


def test_the_parent_never_multiplies_its_OWN_weight():
    """A parent has no pack size, so multiplying at that grain is meaningless rather than risky.

    Built so the two answers differ sharply: summing the sizes gives 953.5 kg, while multiplying
    the parent's 1,133 units by any single size's weight gives 1,133, 566.5 or 2,266 — all
    plausible-looking numbers, none of them the weight that shipped.
    """
    rows = [_row("B0DK1LJ3N3", "P1", units=774), _row("B0CWGXYLT6", "P1", units=359)]
    parent = _parent(rows)
    assert parent["units"] == 1133
    for wrong in (1133 * 1.0, 1133 * 0.5, 1133 * 2.0):
        assert parent["weight_kg"] != wrong, (
            "the parent multiplied its own unit count by a single pack weight"
        )


def test_the_multiplication_is_the_shipment_tabs_and_not_a_second_copy():
    """`line_weight` is imported rather than reimplemented, and it carries the rounding.

    0.15 kg × 200 is `30.000000000000004` in plain float arithmetic — a number that would reach a
    spreadsheet cell. `line_weight` rounds to 3 places for exactly this reason, and a second copy of
    `units * weight` here would be both a second place for the rule and a place without the fix.
    """
    parent = _parent([_row("B0SMALLPCK", "P2", units=200)])
    assert parent["weight_kg"] == 30.0
    assert parent["weight_kg"] == line_weight(200, 0.15)
    # Asserted at source, because a reimplementation that happens to agree on this input would pass
    # every value-based test while being a second home for the rule.
    import inspect
    source = inspect.getsource(logic.size_row)
    assert "line_weight(" in source, "size_row does not use the shipment tab's own multiplication"


# ─── An unknown pack size: excluded AND counted ──────────────────────────────


def test_a_size_with_no_pack_weight_is_EXCLUDED_and_COUNTED():
    """The `shipment_weight` rule, asserted as a FIGURE rather than as a flag.

    The parent must be short by exactly the missing size, and must say how many sizes it is short
    by. Half of that is not enough: a parent summing 1 of its 2 sizes reports a total that looks
    entirely complete, and the count is the only thing that contradicts it.
    """
    rows = [_row("B0DK1LJ3N3", "P1", units=200), _row("B0NOWEIGHT", "P1", units=245)]
    parent = _parent(rows)

    assert parent["weight_kg"] == 200.0, "the unknown size was not excluded from the total"
    assert parent["weight_unknown"] == 1, "the excluded size was not counted"
    assert parent["units"] == 445, "the units must still include the unweighed size"

    by_asin = {s["asin"]: s for s in parent["sizes"]}
    assert by_asin["B0NOWEIGHT"]["weight_kg"] is None
    assert by_asin["B0NOWEIGHT"]["weight_unknown"] == 1
    assert by_asin["B0DK1LJ3N3"]["weight_unknown"] == 0


def test_a_parent_with_NO_usable_weight_reports_a_DASH_rather_than_zero():
    """`None`, not 0.0 — `_ratio`'s discipline applied to a weight.

    A product that sold 245 units of packs the sheet has no weight for has an UNKNOWN weight sold.
    Reporting 0.0 kg beside 245 units is a claim rather than an absence, and it would also rank the
    product as the lightest thing in the portfolio — the same error as a 0% TACOS on a product with
    no sales ranking it the most ad-efficient.
    """
    parent = _parent([_row("B0NOWEIGHT", "P1", units=245)])
    assert parent["weight_kg"] is None
    assert parent["weight_unknown"] == 1
    assert parent["units"] == 245


def test_an_ASIN_the_SHEET_has_never_heard_of_is_KEPT_and_counted():
    """The realistic producer of an unknown weight, since the live sheet has none.

    An unmatched ASIN is already kept and FLAGGED rather than dropped — "a product missing from a
    portfolio review is a product nobody reviews". Its weight is simply not known.
    """
    result = logic.portfolio([_row("B0UNKNOWN1", "P1", units=99)], CATALOGUE,
                             ratings={}, decisions={})
    parent = result["parents"][0]
    assert parent["units"] == 99, "the unmatched ASIN was dropped"
    assert parent["weight_kg"] is None
    assert parent["weight_unknown"] == 1
    assert "B0UNKNOWN1" in result["unmatched_asins"]


def test_zero_units_of_a_known_weight_is_zero_kg_and_NOT_unknown():
    """A size that sold nothing weighs nothing, which is a measurement rather than a gap.

    The distinction matters for the count: treating this as unknown would put "1 row has no pack
    weight" on a parent whose sheet entry is perfectly good, sending the owner to fix a sheet that
    is already correct.
    """
    parent = _parent([_row("B0DK1LJ3N3", "P1", units=0, sales=0.0)])
    assert parent["weight_kg"] == 0.0
    assert parent["weight_unknown"] == 0


# ─── The screen and the file ─────────────────────────────────────────────────


def test_the_weight_column_sorts_and_filters_like_any_other_number():
    """`FIELDS` is what the sort headers and the filter builder read.

    Nulls already sort last in both directions and a null row is already excluded from a numeric
    filter — both correct here for free, and both for the same reason: "the sheet has no weight" is
    not a small weight.
    """
    from pathlib import Path
    source = (Path(__file__).parent.parent / "templates" / "portfolio.html").read_text(
        encoding="utf-8"
    )
    fields = source[source.index("const FIELDS = {"):]
    fields = fields[: fields.index("};")]
    assert "weight_kg:" in fields, "the weight column cannot be sorted or filtered on"


def test_the_workbook_carries_the_weight_on_every_row_type():
    """The file must not disagree with the screen, and it has FOUR row builders.

    Size rows, parent rows, flavour-group rows and the TOTAL row are built separately, so a weight
    cell missed in any one of them shifts that row's trailing columns — the spreadsheet equivalent of
    the header/body column mismatch this tab has already shipped once.
    """
    from app.routers import portfolio as router
    import inspect
    source = inspect.getsource(router.download_portfolio)
    assert source.count("_kg(") >= 4, (
        f"only {source.count('_kg(')} row builders carry a weight cell; there are four "
        "(size, parent, flavour group, TOTAL) plus the subtitle"
    )
    assert '"Weight (kg)"' in source, "the header has no weight column"


def test_the_workbooks_TOTAL_uses_the_aggregate_not_a_resum_of_the_rows():
    """Re-summing `rows` would double-count: each parent row already contains its sizes.

    The same reason `build_portfolio_xlsx` has no `_totals_row` at all — its own docstring says so.
    """
    from app.routers import portfolio as router
    import inspect
    source = inspect.getsource(router.download_portfolio)
    assert 'totals.get("weight_kg")' in source, (
        "the TOTAL row does not use _sum_sizes' own weight figure"
    )


def test_a_dash_and_not_a_zero_reaches_a_spreadsheet_cell():
    """`_kg(None)` must be the em dash, for the reason the screen shows one.

    A workbook leaves the app with no banner beside it, so a 0 here is indistinguishable from a
    measured zero to whoever opens the file next week.
    """
    from app.routers.portfolio import _kg
    assert _kg(None) == "—"
    assert _kg(0.0) == "0.0 kg", "a genuine zero must still read as a measurement"
    assert _kg(953.5) == "953.5 kg"
