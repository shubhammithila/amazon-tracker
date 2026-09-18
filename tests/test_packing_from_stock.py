"""The packer records what he MADE today and what he took off the SHELF. They add up.

Asked for so the packed sheet ops prints for the accounts team can tell the difference:

> "they want a column to mention if they are taking the product which is available in stock — or
>  the qty they are taking from the available stock and the qty they have packed today — so that
>  they can inform the same to the accounts team"

And the arithmetic, which is the whole design:

> "if they pack 40 today and take 50 from available. then total 90 goes to fba packing"

**So `units` keeps its exact existing meaning: the TOTAL boxed.** That is not a stylistic choice.
Every downstream figure already reads it — `remaining_for`, `over_packed`, `_recompute_day_units`,
the hold threshold, the GST invoice quantity, the Amazon upload quantity — and re-pointing all of
them at `units + from_stock` would put a wrong number on a tax document the first time one was
missed. Only the provenance is new, and "made today" is DERIVED.

Every test here fails against the code before this change.
"""
import pytest

from app.shipment import logic

pytestmark = pytest.mark.asyncio


# ── The arithmetic, in the pure functions ────────────────────────────────────


def test_forty_made_plus_fifty_from_stock_is_ninety_to_fba():
    """The sentence the feature was specified with, as an assertion."""
    entry = {"asin": "B01", "units": 90, "from_stock": 50}
    assert logic.made_today(entry) == 40
    assert logic.packed_units([entry]) == 90
    assert logic.from_stock_units([entry]) == 50


def test_made_today_is_derived_not_stored():
    """It is computed from `units` and `from_stock`, so the two ends cannot disagree.

    A stored third number is the defect this codebase records three times: the Orders tab's "86
    orders beside 87 lines", the Portfolio parent rows that exist to prevent it, and the ads
    campaign headers rolled up in `group_changes` rather than in the template.
    """
    from app.models import ShipmentPackingEntry

    columns = {c.name for c in ShipmentPackingEntry.__table__.columns}
    assert "from_stock" in columns
    assert "made_today" not in columns, (
        "made_today must be derived from units - from_stock, never stored"
    )


def test_a_legacy_row_with_no_from_stock_reads_as_all_made():
    """Every row that predates this column was entirely made that day, which is what the data says.

    `server_default="0"` is what makes this true in the database; this asserts the pure functions
    agree, so a row loaded from before the migration is not reported as provenance-unknown.
    """
    assert logic.made_today({"units": 75}) == 75
    assert logic.from_stock_units([{"units": 75}]) == 0


def test_made_today_never_goes_negative():
    """A hand-built request could store from_stock above units; the report must stay readable.

    "Made today: −10" on an accounts sheet reads as a broken report rather than as bad input. The
    screen cannot produce it — the packer types the two parts and the app adds them — so this is
    the backstop, not the guard.
    """
    assert logic.made_today({"units": 40, "from_stock": 50}) == 0


def test_split_by_asin_agrees_with_units_by_asin_about_the_total():
    """Two aggregations, one answer for `units`.

    `split_by_asin` adds the breakdown; it must not change the total, because the packed sheet uses
    it while the screen and the invoice bridge use `units_by_asin`.
    """
    days = [
        {
            "status": logic.STATUS_OPEN,
            "entries": [
                {"asin": "B01", "units": 90, "from_stock": 50},
                {"asin": "B02", "units": 20, "from_stock": 0},
            ],
        },
        {
            "status": logic.STATUS_OPEN,
            "entries": [{"asin": "B01", "units": 10, "from_stock": 10}],
        },
    ]
    split = logic.split_by_asin(days)
    units = logic.units_by_asin(days)

    assert split["B01"]["units"] == units["B01"] == 100
    assert split["B01"]["from_stock"] == 60
    assert split["B01"]["made_today"] == 40
    assert split["B02"]["units"] == units["B02"] == 20
    assert split["B02"]["made_today"] == 20


def test_the_untouched_functions_keep_their_signatures():
    """`remaining_for` and `over_packed` must NOT learn about from_stock.

    They work on the total, which is unchanged. A mutation folding the split into either would
    make the packer's "still needed" and the over-packing warning disagree with the boxes that
    physically exist — and `remaining_for` reaches the printed morning sheet and the Amazon upload
    quantity, where a wrong figure is a short shipment.

    Asserted on the SIGNATURE, the way `still_to_source`'s own test does, because a value-based
    test passes while an unused optional parameter sits there waiting to be wired up.
    """
    import inspect

    assert list(inspect.signature(logic.remaining_for).parameters) == [
        "planned",
        "packed",
    ]
    assert list(inspect.signature(logic.over_packed).parameters) == [
        "planned",
        "packed",
    ]


# ── Through the real routes ──────────────────────────────────────────────────


async def test_the_total_is_stored_as_units_and_the_split_beside_it(
    auth_client, plan_factory, db
):
    """The save path, end to end: 40 + 50 stores units=90."""
    plan = await plan_factory()
    items = await _items(db, plan.id)
    asin = items[0].asin

    response = await auth_client.post(
        "/shipment/packing/2026-09-18",
        json={"entries": [{"asin": asin, "units": 90, "from_stock": 50}], "cartons": 4},
    )
    assert response.status_code == 200

    day = await _day(db, plan.id, "2026-09-18")
    assert day["total_units"] == 90, "units must be the TOTAL that goes to FBA"
    assert day["total_from_stock"] == 50
    assert day["total_made_today"] == 40
    entry = next(e for e in day["entries"] if e["asin"] == asin)
    assert entry["units"] == 90
    assert entry["from_stock"] == 50


async def test_the_hold_threshold_sees_the_total_not_just_what_was_made(
    auth_client, plan_factory, db
):
    """A day of 90 units is 90 for the hold rule, whatever their provenance.

    If the threshold saw only "made today" it would hold a day that has 500 shippable units in
    cartons, and held stock accumulates until someone notices.
    """
    plan = await plan_factory(min_cartons=25, min_units=500)
    items = await _items(db, plan.id)

    await auth_client.post(
        "/shipment/packing/2026-09-18",
        json={
            "entries": [{"asin": items[0].asin, "units": 600, "from_stock": 580}],
            "cartons": 3,
        },
    )
    response = await auth_client.post("/shipment/packing/2026-09-18/submit")
    assert response.status_code == 200
    # 600 total clears the 500-unit minimum even though only 20 were made today.
    assert response.json()["status"] == logic.STATUS_SUBMITTED


async def test_the_GST_invoice_bills_the_TOTAL(auth_client, plan_factory, db):
    """**The test that protects the tax document.**

    `invoice-payload` bills what was packed. If `units` ever came to mean "made today", an invoice
    would under-bill by whatever came off the shelf — and the boxes still ship, so the discrepancy
    surfaces at reconciliation rather than at save time.
    """
    plan = await plan_factory()
    items = await _items(db, plan.id)
    asin = items[0].asin

    await auth_client.post(
        "/shipment/packing/2026-09-18",
        json={"entries": [{"asin": asin, "units": 90, "from_stock": 50}], "cartons": 4},
    )
    await auth_client.post("/shipment/packing/2026-09-18/submit")
    await auth_client.post("/shipment/packing/2026-09-18/verify")

    response = await auth_client.post(
        "/shipment/invoice-payload", json={"pack_dates": ["2026-09-18"]}
    )
    assert response.status_code == 200
    line = next(
        line for line in response.json()["items"] if line.get("asin") == asin
    )
    assert int(line["quantity"]) == 90, (
        "the invoice must bill the TOTAL packed, not just what was made"
    )


async def test_the_amazon_upload_quantity_is_the_TOTAL(auth_client, plan_factory, db):
    """Same rule for what Amazon is told to expect.

    A short quantity here means the FC receives more than the shipment declares, which is a
    discrepancy on their side and a support case on ours.
    """
    plan = await plan_factory()
    items = await _items(db, plan.id)
    asin = items[0].asin

    await auth_client.post(
        "/shipment/packing/2026-09-18",
        json={"entries": [{"asin": asin, "units": 90, "from_stock": 50}], "cartons": 4},
    )
    await auth_client.post("/shipment/packing/2026-09-18/submit")
    await auth_client.post("/shipment/packing/2026-09-18/verify")

    response = await auth_client.post(
        "/shipment/amazon-shipment-preview", json={"pack_dates": ["2026-09-18"]}
    )
    assert response.status_code == 200
    body = response.json()
    line = next(line for line in body["lines"] if line["_asin"] == asin)
    assert line["quantity"] == 90


async def test_from_stock_above_the_total_is_clamped(auth_client, plan_factory, db):
    """The repository refuses to store a value that would make "made today" negative.

    Unreachable from the screen, which sends the sum — but a hand-built request must not be able to
    corrupt the accounts sheet.
    """
    plan = await plan_factory()
    items = await _items(db, plan.id)
    asin = items[0].asin

    await auth_client.post(
        "/shipment/packing/2026-09-18",
        json={"entries": [{"asin": asin, "units": 40, "from_stock": 500}]},
    )
    day = await _day(db, plan.id, "2026-09-18")
    entry = next(e for e in day["entries"] if e["asin"] == asin)
    assert entry["units"] == 40
    assert entry["from_stock"] == 40, "from_stock cannot exceed the total boxed"
    assert day["total_made_today"] == 0


async def test_the_packing_screen_sends_both_parts_and_shows_the_total(
    auth_client, plan_factory, db
):
    """`GET /shipment/packing/{date}` carries `from_stock` and the derived `made_today`.

    The screen needs both to prefill its two inputs; it computes the total itself so the packer
    watches it change as he types.
    """
    plan = await plan_factory()
    items = await _items(db, plan.id)
    asin = items[0].asin

    await auth_client.post(
        "/shipment/packing/2026-09-18",
        json={"entries": [{"asin": asin, "units": 90, "from_stock": 50}]},
    )
    response = await auth_client.get("/shipment/packing/2026-09-18")
    row = next(r for r in response.json()["items"] if r["asin"] == asin)
    assert row["units"] == 90
    assert row["from_stock"] == 50
    assert row["made_today"] == 40


async def test_the_packed_sheet_carries_the_split_for_accounts(
    auth_client, plan_factory, db
):
    """The actual ask: the printout ops hands to accounts.

    Units stays FIRST and stays the total, because that is what accounts reconciles against the
    invoice; the split trails it as the breakdown.
    """
    plan = await plan_factory()
    items = await _items(db, plan.id)

    await auth_client.post(
        "/shipment/packing/2026-09-18",
        json={
            "entries": [{"asin": items[0].asin, "units": 90, "from_stock": 50}],
            "cartons": 4,
        },
    )

    response = await auth_client.get("/shipment/download/packed.xlsx")
    assert response.status_code == 200
    assert len(response.content) > 1000

    from io import BytesIO

    from openpyxl import load_workbook

    sheet = load_workbook(BytesIO(response.content)).active
    rows_out = [row for row in sheet.iter_rows(values_only=True) if row]
    header_index = next(
        i
        for i, row in enumerate(rows_out)
        if "Units" in [str(c) for c in row if c is not None]
    )
    headers = [str(c) for c in rows_out[header_index] if c is not None]
    assert headers[-3:] == ["Units", "Made today", "From stock"], (
        f"the total must come first, then the breakdown: {headers}"
    )

    # **The VALUES must sit under the right headers.** Found by mutation: swapping the two
    # appended cells passed a header-only check, so the accounts sheet would report 50 made and
    # 40 off the shelf — the exact reversal of what happened, on the document that exists to
    # tell them apart.
    #
    # The data row is identified by its ASIN cell rather than by position: the builder appends a
    # TOTAL row whose leading cells are None, and "the first row after the header" would pick up
    # either depending on how many products packed.
    data_row = next(
        row for row in rows_out[header_index + 1:]
        if row and any(str(c or "").startswith("B0") for c in row)
    )
    assert list(data_row[-3:]) == [90, 40, 50], (
        f"Units/Made/From-stock are not in that order: {data_row[-3:]}"
    )


async def test_the_pdf_packed_sheet_renders_with_the_extra_columns(
    auth_client, plan_factory, db
):
    """Two more columns narrow every other one — the PDF builder measures widths from the rows.

    Asserted because `_pdf_column_widths` is computed, and a column count it cannot fit would
    overflow cells into each other: the defect that printed 48-character merchant SKUs on top of
    product names with the whole suite green.
    """
    plan = await plan_factory()
    items = await _items(db, plan.id)

    await auth_client.post(
        "/shipment/packing/2026-09-18",
        json={
            "entries": [{"asin": items[0].asin, "units": 90, "from_stock": 50}],
            "cartons": 4,
        },
    )
    response = await auth_client.get("/shipment/download/packed.pdf")
    assert response.status_code == 200
    assert response.content[:4] == b"%PDF"


async def test_correcting_the_from_stock_figure_takes_effect(
    auth_client, plan_factory, db
):
    """An UPDATE, not only an insert.

    Found by mutation: deleting `row.from_stock = from_stock` passed every other test, because
    they all save a row once. A packer who mistypes the shelf quantity and fixes it would have
    seen his correction silently ignored — the same shape as the `available` column that was
    editable for a whole build and fed nothing.
    """
    plan = await plan_factory()
    items = await _items(db, plan.id)
    asin = items[0].asin

    await auth_client.post(
        "/shipment/packing/2026-09-18",
        json={"entries": [{"asin": asin, "units": 90, "from_stock": 50}]},
    )
    # He realises only 20 came off the shelf, so 70 were made.
    await auth_client.post(
        "/shipment/packing/2026-09-18",
        json={"entries": [{"asin": asin, "units": 90, "from_stock": 20}]},
    )

    day = await _day(db, plan.id, "2026-09-18")
    entry = next(e for e in day["entries"] if e["asin"] == asin)
    assert entry["from_stock"] == 20, "the correction did not take"
    assert day["total_made_today"] == 70


# ── What the BROWSER sends ───────────────────────────────────────────────────
#
# **Three mutations of `templates/ops.html` survived every server-side test**, and this codebase
# has shipped that exact gap three times: the pause feature ("Row … has no usable bid" — 20 tests
# verified the server contract and none checked the client), `intakeFromShipment` (the server sent
# three fields and the screen discarded them), and `renderInvoiceBar` (complete, tested, and
# invisible because its target div did not exist).
#
# Asserted at SOURCE level, because no runtime test here can watch a browser build a payload.


def _ops_source() -> str:
    from pathlib import Path

    return Path("templates/ops.html").read_text(encoding="utf-8")


def _ops_code() -> str:
    """The template with comments stripped.

    The fixes are DOCUMENTED by naming the wrong version, so an assertion that could not coexist
    with its own explanation would force the explanation out — and the explanation is the part
    that stops the next occurrence.
    """
    import re

    source = _ops_source()
    source = re.sub(r"\{#.*?#\}", "", source, flags=re.S)
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    return re.sub(r"^\s*//.*$", "", source, flags=re.M)


def test_the_screen_computes_the_total_as_the_SUM_of_both_inputs():
    """`rowUnits` is made + from stock, and everything reads through it.

    Found by mutation: making `rowUnits` return only `made_today` passed every server test, and it
    is the worst available bug here — the screen would send 40 as the total for 90 boxed units, so
    the GST invoice under-bills and Amazon is told to expect less than arrives.
    """
    code = _ops_code()
    assert (
        'Number(i.made_today || 0) + Number(i.from_stock || 0)' in code
    ), "rowUnits must be the SUM of both inputs"
    # And nothing may read a per-row `units` any more: the screen no longer stores one, so a
    # reader would silently see `undefined` — the defect that printed "100/undefined" on the
    # owner's day columns.
    assert "i.units" not in code and "row.units" not in code, (
        "no per-row `units` field exists on the screen; read rowUnits(i) instead"
    )


def test_the_save_payload_carries_the_total_AND_the_split():
    """Exactly what the pause bug's missing test would have caught.

    The server stores `units` as the total and `from_stock` beside it. If the payload omitted
    either, the server would record the wrong thing with nothing failing.
    """
    code = _ops_code()
    assert "units: rowUnits(i)," in code, (
        "the payload's `units` must be the computed TOTAL"
    )
    assert "from_stock: Number(i.from_stock || 0)," in code, (
        "the payload must carry from_stock, or nothing is ever recorded"
    )


def test_the_screen_asks_for_made_and_from_stock_not_a_total():
    """Two inputs, and no third input for the total.

    The total is computed. A typed total would be a third number for one fact, and it could
    disagree with its own parts.
    """
    code = _ops_code()
    assert 'data-field="made_today"' in code
    assert 'data-field="from_stock"' in code
    assert 'data-field="units"' not in code


# ── helpers ─────────────────────────────────────────────────────────────────


async def _items(db, plan_id):
    from app.shipment import repository

    return await repository.load_plan_items(db, plan_id)


async def _day(db, plan_id, pack_date):
    from app.shipment import repository

    days = await repository.load_days_with_entries(db, plan_id)
    return next(d for d in days if d["pack_date"] == pack_date)
