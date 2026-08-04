"""The four shipment downloads: format, content, and above all row order.

Requirement 3 was "sorted product wise, then weight wise on the dashboard **and
in the downloads as well**". The second half is the part a test has to defend,
because it is invisible: a download with the rows in the wrong order still opens
fine, still totals correctly, and is only noticed when the owner is holding the
printout next to the screen and they disagree.

So the central tests here read the generated xlsx back with openpyxl and assert
its data rows equal ``logic.sort_items`` order. That is a real check on the bytes
that reach the owner, not on the list handed to the builder — a builder that
re-sorted internally would pass the latter and fail the former.

Mutation-verified. With a ``sorted(items, key=lambda i: i["asin"])`` inserted at
the top of ``build_packing_plan_xlsx``, ``test_packing_plan_xlsx_row_order`` and
``test_xlsx_order_survives_a_shuffled_input`` both fail. Without that check the
whole file would only be asserting that openpyxl can write a spreadsheet.
"""
import io

import pytest

from app.shipment import documents, logic
from tests.conftest import CANONICAL_ORDER

pytestmark = pytest.mark.regression


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _read_rows(buffer: io.BytesIO) -> tuple[list[str], list[list]]:
    """(header, data rows) from an xlsx in memory."""
    from openpyxl import load_workbook

    buffer.seek(0)
    workbook = load_workbook(buffer)
    sheet = workbook.active
    rows = [list(r) for r in sheet.iter_rows(values_only=True)]
    return rows[0], rows[1:]


def _item(asin, item, weight, **extra):
    """A plan-item dict in the shape _item_payload produces."""
    row = {
        "asin": asin,
        "item": item,
        "weight": weight,
        "brand": "MF",
        "fba_sku": f"SKU-{asin}",
        "sales_7d": 10,
        "projection": 50,
        "fba_stock": 5,
        "deficit": 45,
        "shipment_plan": 50,
        "available": 0,
        "s": False,
        "m": False,
        "b": False,
        "packed": 0,
        "shippable": 0,
        "remaining": 50,
    }
    row.update(extra)
    return row


@pytest.fixture
def plan():
    return {"id": 1, "label": "Plan 2026-07-30", "min_cartons": 25, "min_units": 500}


@pytest.fixture
def items():
    """Deliberately awkward, mirroring conftest.plan_factory exactly.

    'aloe vera juice' is lowercase and sorts FIRST alphabetically, but it is
    Howrah Foods and category P6 Rest, so it must come LAST. A builder that
    re-sorted by name, by ASIN, case-sensitively, or that ignored brand and
    category, would put it somewhere else — which is what makes the ordering
    assertions below able to fail rather than merely able to pass.
    """
    return logic.sort_items([
        _item("B0AAA00001", "Chana Sattu", 1.0, shipment_plan=500, packed=200, remaining=300),
        _item("B0AAA00002", "Chana Sattu", 0.5, shipment_plan=300, packed=300, remaining=0),
        _item("B0BBB00001", "jau sattu", 1.0, shipment_plan=200, packed=0, remaining=200),
        _item("B0CCC00001", "aloe vera juice", 2.0, brand="HF",
              shipment_plan=100, packed=40, remaining=60),
    ])


@pytest.fixture
def days():
    return [
        {
            "pack_date": "2026-07-28",
            "status": logic.STATUS_VERIFIED,
            "total_units": 240,
            "total_cartons": 30,
            "entries": [
                {"asin": "B0AAA00001", "units": 200, "cartons": 25},
                {"asin": "B0CCC00001", "units": 40, "cartons": 5},
            ],
        },
        {
            "pack_date": "2026-07-29",
            "status": logic.STATUS_HELD,
            "total_units": 300,
            "total_cartons": 20,
            "entries": [{"asin": "B0AAA00002", "units": 300, "cartons": 20}],
        },
    ]


# ─── Row order: the point of the file ────────────────────────────────────────

#: Imported rather than restated. The order lived in three separate hardcoded
#: copies (here, test_shipment_plan_db, test_shipment_download_routes), which is
#: three places to update and three chances to update only two — and a stale copy
#: that still passes is worse than one that fails.
EXPECTED_ORDER = CANONICAL_ORDER
#  MF·P1 chana 0.5kg → MF·P1 chana 1kg → MF·P1 jau 1kg → HF·P6 aloe


def test_the_fixture_itself_is_in_canonical_order(items):
    """Guards the guard. If this drifts, every ordering test below is vacuous."""
    assert [i["asin"] for i in items] == EXPECTED_ORDER


def test_packing_plan_xlsx_row_order(plan, items):
    _header, rows = _read_rows(documents.build_packing_plan_xlsx(plan, items))
    asin_column = 3  # Brand, Product, Weight, ASIN
    assert [r[asin_column] for r in rows] == EXPECTED_ORDER


def test_packed_xlsx_row_order(plan, items, days):
    _header, rows = _read_rows(documents.build_packed_xlsx(plan, items, days))
    assert [r[3] for r in rows] == EXPECTED_ORDER


def test_shipment_file_xlsx_row_order(items):
    _header, rows = _read_rows(documents.build_shipment_file_xlsx(items, mode="all"))
    assert [r[1] for r in rows] == EXPECTED_ORDER  # SKU, ASIN, ...


def test_xlsx_order_survives_a_shuffled_input(plan, items):
    """A builder must render the order it is GIVEN, not one it computes.

    Handed a deliberately wrong order, the output must be equally wrong. That
    sounds perverse but it is the contract: ordering lives in
    ``repository.load_plan_items`` alone, and a builder that re-sorts would mask
    an ordering bug there while introducing its own.
    """
    reversed_items = list(reversed(items))
    _header, rows = _read_rows(documents.build_packing_plan_xlsx(plan, reversed_items))
    assert [r[3] for r in rows] == list(reversed(EXPECTED_ORDER))


def test_sort_items_is_what_the_documents_agree_with(items):
    """The documents' order is `logic.sort_items`, not some private rule."""
    import random

    shuffled = items[:]
    random.Random(7).shuffle(shuffled)
    assert [i["asin"] for i in logic.sort_items(shuffled)] == EXPECTED_ORDER


# ─── Formats ─────────────────────────────────────────────────────────────────

def test_xlsx_files_are_real_xlsx(plan, items, days):
    """xlsx is a zip, so it must start with the PK magic bytes."""
    for buffer in (
        documents.build_packing_plan_xlsx(plan, items),
        documents.build_packed_xlsx(plan, items, days),
        documents.build_shipment_file_xlsx(items),
    ):
        buffer.seek(0)
        assert buffer.read(2) == b"PK"


def test_pdf_files_are_real_pdf(plan, items):
    for buffer in (
        documents.build_packing_plan_pdf(plan, items),
        documents.build_remaining_pdf(plan, items),
    ):
        buffer.seek(0)
        assert buffer.read(4) == b"%PDF"


def test_buffers_are_rewound_ready_to_stream(plan, items):
    """StreamingResponse reads from the current position — an un-rewound buffer
    would send a 0-byte file that Excel reports as corrupt."""
    assert documents.build_packing_plan_xlsx(plan, items).tell() == 0
    assert documents.build_packing_plan_pdf(plan, items).tell() == 0


# ─── Packing plan content ────────────────────────────────────────────────────

def test_packing_plan_xlsx_has_a_row_per_item_and_a_header(plan, items):
    header, rows = _read_rows(documents.build_packing_plan_xlsx(plan, items))
    assert len(rows) == len(items)
    assert header[0] == "Brand"
    assert "To Pack" in header


def test_packing_plan_xlsx_carries_the_numbers_not_just_the_names(plan, items):
    header, rows = _read_rows(documents.build_packing_plan_xlsx(plan, items))
    chana_1kg = next(r for r in rows if r[3] == "B0AAA00001")
    assert chana_1kg[header.index("To Ship")] == 500
    assert chana_1kg[header.index("Packed")] == 200
    assert chana_1kg[header.index("To Pack")] == 300


def test_the_plan_sheet_shows_stock_on_hand_and_what_is_left_to_make(plan, items):
    """The In-stock figure has to leave the screen, or it is decorative again.

    The owner plans production from this sheet. "To Pack" answers what the packer
    must box (ignoring warehouse stock, which is not in a carton yet); "To Make"
    answers what he must produce. Both are needed and they are different numbers.
    """
    header, rows = _read_rows(documents.build_packing_plan_xlsx(plan, items))
    assert "In Stock" in header, "the plan sheet does not carry the In-stock figure"
    assert "To Make" in header, "the plan sheet does not say what is left to produce"


def test_weight_label_trims_the_pointless_zero():
    assert documents._weight_label(1.0) == "1kg"
    assert documents._weight_label(0.5) == "0.5kg"
    assert documents._weight_label(2.25) == "2.25kg"


@pytest.mark.parametrize("value", [None, 0, "", "abc", -1])
def test_weight_label_is_blank_rather_than_wrong(value):
    """A missing weight prints nothing. '0kg' on a warehouse sheet reads as a
    real fact about the product and would send someone looking for the 0kg bag."""
    assert documents._weight_label(value) == ""


def test_size_flags_render_as_letters():
    assert documents._flags({"s": True, "m": False, "b": True}) == "SB"
    assert documents._flags({}) == ""


# ─── Packed daily sheet: requirements 6 and 7 ────────────────────────────────

def test_packed_xlsx_has_a_units_and_cartons_column_per_day(plan, items, days):
    """Requirement 7: cartons are recorded daily, so they must be downloadable
    per day — a single total tells the owner nothing about how to box a shipment.

    The 28th is verified so its columns are unsuffixed; the 29th is held so its
    columns carry a label. Both must exist.
    """
    header, _rows = _read_rows(documents.build_packed_xlsx(plan, items, days))
    assert "2026-07-28 Units" in header
    assert "2026-07-28 Cartons" in header
    assert any(h and h.startswith("2026-07-29 Units") for h in header)
    assert any(h and h.startswith("2026-07-29 Cartons") for h in header)


@pytest.mark.parametrize(
    "status,labelled",
    [
        (logic.STATUS_OPEN, True),
        (logic.STATUS_HELD, True),
        (logic.STATUS_SUBMITTED, False),
        (logic.STATUS_VERIFIED, False),
        (logic.STATUS_SHIPPED, False),
    ],
)
def test_only_unsettled_days_are_labelled(plan, items, status, labelled):
    """Open and held are the two states where the numbers must not be read as
    final — open because ops is still typing, held because the day is parked.
    The other three are normal, and labelling them would be noise on a sheet
    that already grows two columns a day."""
    day = [{"pack_date": "2026-07-28", "status": status, "entries": []}]
    header, _rows = _read_rows(documents.build_packed_xlsx(plan, items, day))
    columns = [h for h in header if h and h.startswith("2026-07-28")]
    assert len(columns) == 2, columns
    if labelled:
        assert all(f"({status})" in h for h in columns), columns
    else:
        assert columns == ["2026-07-28 Units", "2026-07-28 Cartons"], columns


def test_packed_xlsx_columns_are_chronological(plan, items):
    """Dates arrive from load_days ordered; the sheet must not reorder them,
    or the owner reads Tuesday's numbers under Monday's heading."""
    many = [
        {"pack_date": d, "status": logic.STATUS_SUBMITTED, "entries": []}
        for d in ("2026-07-27", "2026-07-28", "2026-07-29")
    ]
    header, _rows = _read_rows(documents.build_packed_xlsx(plan, items, many))
    positions = [header.index(f"{d} Units") for d in ("2026-07-27", "2026-07-28", "2026-07-29")]
    assert positions == sorted(positions)


def test_packed_xlsx_labels_held_days(plan, items, days):
    """A held day's boxes exist, so the sheet shows them — but unlabelled they
    would read as ready to ship, and the owner would count them into a shipment
    that has not been cleared."""
    header, _rows = _read_rows(documents.build_packed_xlsx(plan, items, days))
    held = [h for h in header if h and h.startswith("2026-07-29")]
    assert held, "the held day is missing from the sheet entirely"
    assert all("held" in h for h in held), held


def test_packed_xlsx_separates_total_from_shippable(plan, items, days):
    """The crux of requirement 9. Chana 0.5kg's 300 units were packed on a HELD
    day: they are packed (do not re-pack them) but not shippable (not cleared).
    One number for both is the bug this whole feature exists to prevent.
    """
    header, rows = _read_rows(documents.build_packed_xlsx(plan, items, days))
    row = next(r for r in rows if r[3] == "B0AAA00002")
    assert row[header.index("Total Units")] == 300
    assert row[header.index("Shippable Units")] == 0, (
        "held units leaked into shippable — they would be shipped twice"
    )
    assert row[header.index("To Pack")] == 0, (
        "held units were not counted as packed — the floor would pack them again"
    )


def test_packed_xlsx_counts_verified_days_as_shippable(plan, items, days):
    header, rows = _read_rows(documents.build_packed_xlsx(plan, items, days))
    row = next(r for r in rows if r[3] == "B0AAA00001")
    assert row[header.index("Total Units")] == 200
    assert row[header.index("Shippable Units")] == 200


def test_packed_xlsx_writes_zero_for_an_untouched_sku(plan, items, days):
    """Not blank. A SKU untouched on a day must hold its column position, or
    every later date's numbers shift one column left."""
    header, rows = _read_rows(documents.build_packed_xlsx(plan, items, days))
    jau = next(r for r in rows if r[3] == "B0BBB00001")
    assert jau[header.index("2026-07-28 Units")] == 0
    assert jau[header.index("2026-07-28 Cartons")] == 0


def test_packed_xlsx_totals_across_days(plan, items):
    two_days = [
        {
            "pack_date": "2026-07-28",
            "status": logic.STATUS_VERIFIED,
            "entries": [{"asin": "B0AAA00001", "units": 120, "cartons": 10}],
        },
        {
            "pack_date": "2026-07-29",
            "status": logic.STATUS_VERIFIED,
            "entries": [{"asin": "B0AAA00001", "units": 80, "cartons": 7}],
        },
    ]
    header, rows = _read_rows(documents.build_packed_xlsx(plan, items, two_days))
    row = next(r for r in rows if r[3] == "B0AAA00001")
    assert row[header.index("Total Units")] == 200
    assert row[header.index("Total Cartons")] == 17


def test_packed_xlsx_works_before_anyone_has_packed(plan, items):
    """Day one of a plan. An empty day list must produce a usable sheet, not a
    crash — the owner downloads this to see the targets before packing starts."""
    header, rows = _read_rows(documents.build_packed_xlsx(plan, items, []))
    assert len(rows) == len(items)
    assert header[-1] == "To Pack"


# ─── Shipment file: the Amazon upload ────────────────────────────────────────

def test_shipment_file_omits_zero_quantities(items):
    """Chana 0.5kg is fully packed, so nothing remains to plan for it."""
    _header, rows = _read_rows(documents.build_shipment_file_xlsx(items, mode="remaining"))
    assert "B0AAA00002" not in [r[1] for r in rows]
    assert len(rows) == 3


def test_shipment_file_mode_all_uses_the_planned_quantity(items):
    header, rows = _read_rows(documents.build_shipment_file_xlsx(items, mode="all"))
    quantities = {r[1]: r[header.index("Quantity")] for r in rows}
    assert quantities["B0AAA00001"] == 500
    assert quantities["B0AAA00002"] == 300


def test_shipment_file_mode_remaining_uses_what_is_left(items):
    header, rows = _read_rows(documents.build_shipment_file_xlsx(items, mode="remaining"))
    quantities = {r[1]: r[header.index("Quantity")] for r in rows}
    assert quantities["B0AAA00001"] == 300


def test_shipment_file_mode_verified_only_counts_verified_days(items, days):
    """mode=verified is what may legally be invoiced. The held day's 300 units
    must not appear even though they are packed."""
    header, rows = _read_rows(
        documents.build_shipment_file_xlsx(items, mode="verified", days=days)
    )
    quantities = {r[1]: r[header.index("Quantity")] for r in rows}
    assert quantities == {"B0AAA00001": 200, "B0CCC00001": 40}
    assert "B0AAA00002" not in quantities, "held units would be invoiced"


def test_shipment_file_mode_verified_is_empty_when_nothing_is_verified(items):
    submitted_only = [
        {
            "pack_date": "2026-07-28",
            "status": logic.STATUS_SUBMITTED,
            "entries": [{"asin": "B0AAA00001", "units": 200, "cartons": 20}],
        }
    ]
    _header, rows = _read_rows(
        documents.build_shipment_file_xlsx(items, mode="verified", days=submitted_only)
    )
    assert rows == [], "submitted is not verified — the owner has not approved it"


def test_shipment_file_leaves_a_missing_sku_blank(items):
    """Amazon's upload keys on the merchant SKU. Falling back to the ASIN
    produces a file that looks right and is rejected on their side; a visible
    blank is a problem the owner can fix before uploading."""
    no_sku = [dict(i, fba_sku="") for i in items]
    header, rows = _read_rows(documents.build_shipment_file_xlsx(no_sku, mode="all"))
    sku_column = header.index("Merchant SKU")
    for row in rows:
        assert row[sku_column] in (None, ""), row
        assert row[sku_column] != row[header.index("ASIN")]


def test_shipment_file_warns_about_missing_skus(items, caplog):
    """The old code swallowed this in a bare except. A silent version of this
    failure is the reason it is a logged warning and a counted number."""
    import logging

    no_sku = [dict(i, fba_sku="") for i in items]
    with caplog.at_level(logging.WARNING, logger="app.shipment.documents"):
        documents.build_shipment_file_xlsx(no_sku, mode="all")
    # getMessage(), not `.message % .args` — the record is logged lazily, so the
    # args are still separate and `%`-ing an already-interpolated string raises.
    messages = [r.getMessage() for r in caplog.records]
    assert any("merchant SKU" in m for m in messages), messages
    assert any("4 row(s)" in m for m in messages), (
        f"the count is the useful part of the warning: {messages}"
    )


def test_shipment_file_is_quiet_when_every_sku_is_present(items, caplog):
    import logging

    with caplog.at_level(logging.WARNING, logger="app.shipment.documents"):
        documents.build_shipment_file_xlsx(items, mode="all")
    assert not caplog.records


# ─── Remaining PDF: the morning clipboard sheet ──────────────────────────────

def test_remaining_pdf_is_a_pdf(plan, items):
    buffer = documents.build_remaining_pdf(plan, items, pack_date="2026-07-30")
    assert buffer.read(4) == b"%PDF"


def test_remaining_pdf_handles_a_fully_packed_plan(plan, items):
    """Requirement 5's feedback loop: the list shrinks each morning, and when
    everything is done it must say so rather than render an empty table or
    crash on a table with only a header."""
    done = [dict(i, remaining=0) for i in items]
    buffer = documents.build_remaining_pdf(plan, done)
    assert buffer.read(4) == b"%PDF"
    assert buffer.getbuffer().nbytes > 500


def test_remaining_pdf_survives_an_empty_plan(plan):
    buffer = documents.build_remaining_pdf(plan, [])
    assert buffer.read(4) == b"%PDF"


def test_remaining_pdf_defaults_the_date(plan, items):
    """Called with no date it must still build — the ops screen may not send one."""
    assert documents.build_remaining_pdf(plan, items).read(4) == b"%PDF"


# ─── Shared styling ──────────────────────────────────────────────────────────

def test_excel_and_pdf_headers_are_the_same_colour():
    """HEADER_HEX is derived from HEADER_RGB so the two cannot drift apart.

    334C4C, not 334D4D: 0.3 * 255 is 76.5 and Python's round() takes halves to
    the even number, so 76. Exactly the banker's-rounding trap that made
    logic.round_to_step use Decimal — here it is one imperceptible shade of grey
    rather than a wrong shipment quantity, so it is left alone and written down.
    """
    assert documents.HEADER_HEX == "334C4C"
    # The invoice PDFs use the float form; both documents must read as one product.
    assert documents.HEADER_RGB == (0.2, 0.3, 0.3)


def test_xlsx_freezes_the_header_row(plan, items, days):
    """The packed sheet grows a column pair per day; by Friday the owner is
    scrolling and would lose track of which column is which date."""
    from openpyxl import load_workbook

    buffer = documents.build_packed_xlsx(plan, items, days)
    buffer.seek(0)
    assert load_workbook(buffer).active.freeze_panes == "A2"


# ─── Robustness against real-world rows ──────────────────────────────────────

def test_builders_tolerate_missing_keys(plan):
    """Rows come from the DB via _item_payload, but a legacy-imported plan can
    carry Nones. A download must not 500 on one blank field."""
    sparse = [{"asin": "B0XXX00001"}]
    assert documents.build_packing_plan_xlsx(plan, sparse).read(2) == b"PK"
    assert documents.build_packed_xlsx(plan, sparse, []).read(2) == b"PK"
    assert documents.build_packing_plan_pdf(plan, sparse).read(4) == b"%PDF"
    assert documents.build_remaining_pdf(plan, sparse).read(4) == b"%PDF"


def test_builders_tolerate_a_plan_with_no_label():
    """`plan.get('label') or 'Plan'` — a blank title must not print 'None'."""
    buffer = documents.build_packing_plan_pdf({}, [_item("B0A", "X", 1.0)])
    assert buffer.read(4) == b"%PDF"
