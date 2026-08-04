"""The five download routes over HTTP: wiring, gating, and end-to-end ordering.

tests/test_shipment_documents.py proves the builders are correct in isolation.
This file proves they are actually reachable, correctly gated by role, and — the
part neither file can check alone — that the rows arriving in a downloaded xlsx
come out in the order the DB's single ORDER BY produced.

That last one is the real regression guard for requirement 3. The unit tests
assert "the builder renders what it is given"; these assert "what it is given is
the canonical order". Both halves are needed: with only the unit tests, a router
that passed items in insertion order would pass everything.
"""
import io

import pytest

from app.shipment import logic
from tests.conftest import CANONICAL_ORDER

pytestmark = pytest.mark.regression

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

ADMIN_DOWNLOADS = [
    "/shipment/download/packing-plan.xlsx",
    "/shipment/download/packing-plan.pdf",
    "/shipment/download/packed.xlsx",
    "/shipment/download/shipment-file.xlsx",
]
OPS_DOWNLOADS = ["/shipment/download/remaining.pdf"]
ALL_DOWNLOADS = ADMIN_DOWNLOADS + OPS_DOWNLOADS


def _rows(content: bytes):
    """(header, data rows) from downloaded xlsx bytes."""
    from openpyxl import load_workbook

    sheet = load_workbook(io.BytesIO(content)).active
    rows = [list(r) for r in sheet.iter_rows(values_only=True)]
    return rows[0], rows[1:]


# ─── Wiring ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("path", ALL_DOWNLOADS)
async def test_downloads_work_for_admin(auth_client, plan_factory, path):
    await plan_factory()
    r = await auth_client.get(path)
    assert r.status_code == 200, f"{path} -> {r.status_code}: {r.text[:200]}"
    assert r.content, f"{path} returned an empty body"


@pytest.mark.parametrize("path", ALL_DOWNLOADS)
async def test_downloads_are_attachments_with_a_dated_filename(
    auth_client, plan_factory, path
):
    """Content-Disposition: a browser must save these, not try to render the
    xlsx as text. The date in the name stops next week's download overwriting
    this week's in the Downloads folder."""
    await plan_factory()
    r = await auth_client.get(path)
    disposition = r.headers.get("content-disposition", "")
    assert disposition.startswith("attachment;"), disposition
    assert "2026-" in disposition, f"no date in the filename: {disposition}"


@pytest.mark.parametrize("path", ADMIN_DOWNLOADS[:1] + ["/shipment/download/packed.xlsx"])
async def test_xlsx_routes_send_the_spreadsheet_content_type(
    auth_client, plan_factory, path
):
    await plan_factory()
    r = await auth_client.get(path)
    assert r.headers["content-type"].startswith(XLSX), r.headers["content-type"]
    assert r.content[:2] == b"PK"


@pytest.mark.parametrize(
    "path", ["/shipment/download/packing-plan.pdf", "/shipment/download/remaining.pdf"]
)
async def test_pdf_routes_send_pdf(auth_client, plan_factory, path):
    await plan_factory()
    r = await auth_client.get(path)
    assert r.headers["content-type"].startswith("application/pdf")
    assert r.content[:4] == b"%PDF"


# ─── Role gating ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("path", ADMIN_DOWNLOADS)
async def test_ops_cannot_download_the_owner_documents(ops_client, plan_factory, path):
    """The packer gets the morning sheet and nothing else. The plan carries
    projections and the shipment file is an upload to Amazon — both are the
    owner's decisions, not the warehouse's."""
    await plan_factory()
    r = await ops_client.get(path)
    assert r.status_code == 403, f"ops downloaded {path} ({r.status_code})"


async def test_ops_can_download_the_morning_sheet(ops_client, plan_factory):
    """Requirement 5 is only met if the packer can fetch this himself. Making
    him ask the owner every morning defeats the point of his own screen."""
    await plan_factory()
    r = await ops_client.get("/shipment/download/remaining.pdf")
    assert r.status_code == 200, r.status_code
    assert r.content[:4] == b"%PDF"


@pytest.mark.parametrize("path", ALL_DOWNLOADS)
async def test_signed_out_users_get_nothing(client, plan_factory, path):
    await plan_factory()
    r = await client.get(path)
    assert r.status_code in (303, 401, 403), r.status_code
    assert r.content[:2] != b"PK"
    assert r.content[:4] != b"%PDF"


# ─── No plan yet ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("path", ALL_DOWNLOADS)
async def test_downloads_404_before_a_plan_exists(auth_client, path):
    """A 404 with a message, not a 500 and not a zero-byte file. An empty
    spreadsheet that Excel calls corrupt looks like a broken app; "no active
    plan" tells the owner to upload the CSVs."""
    r = await auth_client.get(path)
    assert r.status_code == 404, r.status_code
    assert "plan" in r.json()["error"].lower()


# ─── End-to-end row order: requirement 3 ─────────────────────────────────────

async def test_downloaded_xlsx_order_matches_the_canonical_order(
    auth_client, plan_factory, read_committed
):
    """The whole point. The bytes the owner receives must be in the same order as
    the screen, which is repository.load_plan_items' ORDER BY, which is
    logic.sort_items.

    The plan_factory fixture includes 'aloe vera juice' — lowercase, first
    alphabetically, but Howrah Foods and category P6 — precisely so that dropping
    the brand rank, the category rank OR the casefold anywhere in this path
    produces a different order and fails here.
    """
    from app.shipment import repository

    plan = await plan_factory()
    plan_id = plan.id

    items = await read_committed(repository.load_plan_items, plan_id)
    expected = [i.asin for i in items]
    # Guard the guard: if the DB order were already wrong this test would happily
    # compare wrong against wrong.
    assert expected == [i.asin for i in logic.sort_items(items)]
    assert expected == CANONICAL_ORDER, (
        f"the DB order is not canonical, so this test would compare wrong against "
        f"wrong: {expected}"
    )
    assert expected[-1] == "B0CCC00001", (
        f"the HF/P6 row should sort last despite being first alphabetically, got "
        f"{expected}"
    )

    r = await auth_client.get("/shipment/download/packing-plan.xlsx")
    _header, rows = _rows(r.content)
    assert [row[3] for row in rows] == expected


async def test_packed_xlsx_order_matches_too(auth_client, plan_factory, read_committed):
    from app.shipment import repository

    plan = await plan_factory()
    plan_id = plan.id
    await auth_client.post(
        "/shipment/packing/2026-07-30",
        json={"entries": [{"asin": "B0AAA00001", "units": 100, "cartons": 8}]},
    )

    items = await read_committed(repository.load_plan_items, plan_id)
    r = await auth_client.get("/shipment/download/packed.xlsx")
    _header, rows = _rows(r.content)
    assert [row[3] for row in rows] == [i.asin for i in items]


async def test_shipment_file_order_matches_the_canonical_order(
    auth_client, plan_factory, read_committed
):
    """The shipment file drops zero-quantity rows, so its order is the canonical
    order with gaps — never a re-sort of what is left."""
    from app.shipment import repository

    plan = await plan_factory()
    plan_id = plan.id
    items = await read_committed(repository.load_plan_items, plan_id)
    expected = [i.asin for i in items if int(i.shipment_plan or 0) > 0]

    r = await auth_client.get("/shipment/download/shipment-file.xlsx?mode=all")
    _header, rows = _rows(r.content)
    assert [row[1] for row in rows] == expected


# ─── Numbers agree with the screen ───────────────────────────────────────────

async def test_downloaded_numbers_match_the_dashboard(auth_client, plan_factory):
    """Same source, so they must agree. /active and the download both build rows
    through _item_payload; if someone later gives the download its own
    calculation, this fails."""
    await plan_factory()
    await auth_client.post(
        "/shipment/packing/2026-07-30",
        json={"entries": [{"asin": "B0AAA00001", "units": 120, "cartons": 10}]},
    )

    active = (await auth_client.get("/shipment/active")).json()
    screen = {i["asin"]: i for i in active["items"]}

    r = await auth_client.get("/shipment/download/packing-plan.xlsx")
    header, rows = _rows(r.content)
    for row in rows:
        item = screen[row[3]]
        assert row[header.index("To Ship")] == item["shipment_plan"]
        assert row[header.index("Packed")] == item["packed"]
        assert row[header.index("To Pack")] == item["remaining"]
        # In Stock and To Make travel too. The In-stock figure fed nothing at all
        # before this change, so a download that quietly dropped it would put the
        # column straight back to being decorative.
        assert row[header.index("In Stock")] == item["available"]
        assert row[header.index("To Make")] == item["to_source"]


def _column(header: list, date_str: str, kind: str) -> int:
    """Index of a per-day column, whatever status label it carries.

    Day columns are titled '<date> Units' plus a status suffix for days that are
    not yet settled — '2026-07-30 Units (open)'. Tests match on the prefix so
    they assert about the day's numbers rather than about its label; the labels
    themselves are asserted deliberately, in their own tests.
    """
    prefix = f"{date_str} {kind}"
    matches = [i for i, h in enumerate(header) if h and h.startswith(prefix)]
    assert len(matches) == 1, f"expected one {prefix!r} column, got {matches} in {header}"
    return matches[0]


async def test_packed_download_carries_the_cartons(auth_client, plan_factory):
    """Requirement 7: cartons entered daily must come out in the Excel, because
    they are what prefills the invoice's Boxes field."""
    await plan_factory()
    await auth_client.post(
        "/shipment/packing/2026-07-30",
        json={"entries": [{"asin": "B0AAA00001", "units": 120, "cartons": 10}]},
    )
    r = await auth_client.get("/shipment/download/packed.xlsx")
    header, rows = _rows(r.content)
    row = next(x for x in rows if x[3] == "B0AAA00001")
    assert row[_column(header, "2026-07-30", "Units")] == 120
    assert row[_column(header, "2026-07-30", "Cartons")] == 10
    assert row[header.index("Total Cartons")] == 10


async def test_an_unsubmitted_day_is_labelled_open_in_the_download(
    auth_client, plan_factory
):
    """Ops is still entering, so those numbers are not final. Unlabelled they
    would read the same as a submitted day and the owner could build a shipment
    from half a day's packing."""
    await plan_factory()
    await auth_client.post(
        "/shipment/packing/2026-07-30",
        json={"entries": [{"asin": "B0AAA00001", "units": 120, "cartons": 10}]},
    )
    header, _rows_ = _rows((await auth_client.get("/shipment/download/packed.xlsx")).content)
    day_columns = [h for h in header if h and h.startswith("2026-07-30")]
    assert day_columns, "the day is missing from the sheet"
    assert all("(open)" in h for h in day_columns), day_columns


async def test_a_submitted_day_carries_no_status_label(auth_client, plan_factory):
    """Submitted is the normal, ready state — labelling it would be noise on a
    sheet that already has a column pair per day."""
    await plan_factory()
    await auth_client.post(
        "/shipment/packing/2026-07-30",
        json={"entries": [{"asin": "B0AAA00001", "units": 500, "cartons": 40}]},
    )
    submitted = await auth_client.post("/shipment/packing/2026-07-30/submit")
    assert submitted.json()["status"] == logic.STATUS_SUBMITTED, submitted.json()

    header, _rows_ = _rows((await auth_client.get("/shipment/download/packed.xlsx")).content)
    assert "2026-07-30 Units" in header
    assert "2026-07-30 Cartons" in header


async def test_held_units_are_packed_but_not_shippable_in_the_download(
    auth_client, plan_factory
):
    """A 20-carton/400-unit day is held. Requirement 9 end-to-end: the download
    must show those units as packed (do not re-pack them) and NOT as shippable."""
    await plan_factory()
    await auth_client.post(
        "/shipment/packing/2026-07-30",
        json={"entries": [{"asin": "B0AAA00001", "units": 400, "cartons": 20}]},
    )
    submit = await auth_client.post("/shipment/packing/2026-07-30/submit")
    assert submit.json()["status"] == logic.STATUS_HELD, submit.json()

    r = await auth_client.get("/shipment/download/packed.xlsx")
    header, rows = _rows(r.content)
    row = next(x for x in rows if x[3] == "B0AAA00001")
    assert row[_column(header, "2026-07-30", "Units")] == 400
    assert row[header.index("Total Units")] == 400
    assert row[header.index("Shippable Units")] == 0
    held_columns = [h for h in header if h and h.startswith("2026-07-30")]
    assert all("held" in h for h in held_columns), held_columns


async def test_releasing_a_held_day_makes_its_units_shippable(auth_client, plan_factory):
    """The owner overrides the threshold, and the download must follow."""
    await plan_factory()
    await auth_client.post(
        "/shipment/packing/2026-07-30",
        json={"entries": [{"asin": "B0AAA00001", "units": 400, "cartons": 20}]},
    )
    await auth_client.post("/shipment/packing/2026-07-30/submit")
    released = await auth_client.post("/shipment/packing/2026-07-30/release")
    assert released.status_code == 200, released.text

    r = await auth_client.get("/shipment/download/packed.xlsx")
    header, rows = _rows(r.content)
    row = next(x for x in rows if x[3] == "B0AAA00001")
    assert row[header.index("Shippable Units")] == 400


# ─── mode= on the shipment file ──────────────────────────────────────────────

@pytest.mark.parametrize("mode", ["remaining", "all", "verified"])
async def test_every_documented_mode_is_accepted(auth_client, plan_factory, mode):
    await plan_factory()
    r = await auth_client.get(f"/shipment/download/shipment-file.xlsx?mode={mode}")
    assert r.status_code == 200, r.text[:200]


@pytest.mark.parametrize("mode", ["Remaining", "verifed", "everything", ""])
async def test_an_unknown_mode_is_rejected_not_guessed(auth_client, plan_factory, mode):
    """A typo must not silently fall back to `remaining`. The three modes give
    genuinely different quantities, and a plausible-looking file with the wrong
    numbers gets uploaded to Amazon before anyone notices."""
    await plan_factory()
    r = await auth_client.get(f"/shipment/download/shipment-file.xlsx?mode={mode}")
    assert r.status_code == 400, f"mode={mode!r} -> {r.status_code}"
    assert "mode" in r.json()["error"]


async def test_mode_verified_excludes_unverified_packing(auth_client, plan_factory):
    """Only the owner's approval may reach an invoice."""
    await plan_factory()
    await auth_client.post(
        "/shipment/packing/2026-07-30",
        json={"entries": [{"asin": "B0AAA00001", "units": 500, "cartons": 40}]},
    )
    await auth_client.post("/shipment/packing/2026-07-30/submit")

    r = await auth_client.get("/shipment/download/shipment-file.xlsx?mode=verified")
    _header, rows = _rows(r.content)
    assert rows == [], "submitted-but-unverified units reached the shipment file"

    await auth_client.post("/shipment/packing/2026-07-30/verify")
    r = await auth_client.get("/shipment/download/shipment-file.xlsx?mode=verified")
    header, rows = _rows(r.content)
    assert [row[1] for row in rows] == ["B0AAA00001"]
    assert rows[0][header.index("Quantity")] == 500


async def test_mode_remaining_shrinks_as_packing_is_recorded(auth_client, plan_factory):
    await plan_factory()
    before = await auth_client.get("/shipment/download/shipment-file.xlsx?mode=remaining")
    header, rows = _rows(before.content)
    original = next(r[header.index("Quantity")] for r in rows if r[1] == "B0AAA00001")

    await auth_client.post(
        "/shipment/packing/2026-07-30",
        json={"entries": [{"asin": "B0AAA00001", "units": 200, "cartons": 15}]},
    )
    after = await auth_client.get("/shipment/download/shipment-file.xlsx?mode=remaining")
    header, rows = _rows(after.content)
    now = next(r[header.index("Quantity")] for r in rows if r[1] == "B0AAA00001")
    assert now == original - 200


# ─── The morning sheet ───────────────────────────────────────────────────────

async def test_remaining_pdf_accepts_a_date(ops_client, plan_factory):
    await plan_factory()
    r = await ops_client.get("/shipment/download/remaining.pdf?pack_date=2026-07-30")
    assert r.status_code == 200
    assert "2026-07-30" in r.headers["content-disposition"]


async def test_remaining_pdf_rejects_a_malformed_date(ops_client, plan_factory):
    await plan_factory()
    r = await ops_client.get("/shipment/download/remaining.pdf?pack_date=30-07-2026")
    assert r.status_code == 400, r.status_code


async def test_remaining_pdf_shrinks_once_a_sku_is_fully_packed(
    ops_client, auth_client, plan_factory
):
    """Requirement 5's feedback loop, end to end: the packer records 500 units of
    the 1kg Chana, and tomorrow's sheet no longer lists it. A smaller PDF is a
    crude proxy but it is the observable one, and 'fewer rows' is the actual
    promise the document makes."""
    await plan_factory()
    before = await ops_client.get("/shipment/download/remaining.pdf")

    await auth_client.post(
        "/shipment/packing/2026-07-30",
        json={"entries": [{"asin": "B0AAA00001", "units": 500, "cartons": 40}]},
    )
    after = await ops_client.get("/shipment/download/remaining.pdf")

    assert len(after.content) < len(before.content), (
        "the fully-packed SKU did not drop off the morning sheet"
    )
