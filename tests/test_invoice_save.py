"""Regression: ISSUE-002 — /invoice/save accepted anything and burned GST numbers.

Found by /qa on 2026-07-30.
Report: .gstack/qa-reports/qa-report-amazon-tracker-2026-07-30.md

GST invoice numbers are a legally sequential series. Before the fix, POST
/invoice/save performed no validation at all:

  * an empty ``{}`` body persisted a ₹0 invoice and permanently advanced the
    series (reproduced live: created ST/26-27/032 with 0 items, then next-number
    reported 033)
  * a user-supplied ``invoice_no`` was trusted blindly, so it could collide with
    an existing row and surface as a raw IntegrityError 500
  * quantity/rate arrived from a JSON form as strings and could raise TypeError
    mid-request

The invariant these tests protect: **a rejected invoice must not consume a
number.** Every rejection case below asserts the sequence is unchanged, not just
that the status code was 400.
"""
import pytest
from sqlalchemy import func, select

from app.models import Invoice

pytestmark = pytest.mark.regression


async def _next_number(auth_client) -> str:
    r = await auth_client.get("/invoice/next-number")
    assert r.status_code == 200, r.text
    return r.json()["next_number"]


async def _invoice_count(db) -> int:
    return (await db.execute(select(func.count()).select_from(Invoice))).scalar()


# ─── Rejections must not consume an invoice number ───────────────────────────

@pytest.mark.parametrize(
    "payload,reason",
    [
        ({}, "empty object — the exact body that burned ST/26-27/032"),
        ({"items": []}, "explicitly empty items list"),
        ({"items": None}, "null items"),
        ({"details": {"shipment_id": "X", "date": "2026-07-30"}}, "details but no items"),
        ({"items": "not-a-list", "details": {"shipment_id": "X", "date": "2026-07-30"}},
         "items is a string"),
    ],
)
async def test_invoice_without_items_is_rejected_and_burns_no_number(
    auth_client, db, payload, reason
):
    before_next = await _next_number(auth_client)
    before_count = await _invoice_count(db)

    r = await auth_client.post("/invoice/save", json=payload)

    assert r.status_code == 400, f"{reason}: expected 400, got {r.status_code} {r.text[:200]}"
    assert await _invoice_count(db) == before_count, f"{reason}: a row was persisted"
    assert await _next_number(auth_client) == before_next, (
        f"{reason}: the GST invoice number advanced despite rejection"
    )


@pytest.mark.parametrize(
    "missing_field", ["shipment_id", "date"],
)
async def test_missing_required_details_field_is_rejected(
    auth_client, db, valid_invoice_payload, missing_field
):
    """shipment_id and date both appear on the printed invoice; neither is optional."""
    payload = dict(valid_invoice_payload)
    payload["details"] = {k: v for k, v in payload["details"].items() if k != missing_field}

    before_next = await _next_number(auth_client)
    r = await auth_client.post("/invoice/save", json=payload)

    assert r.status_code == 400, r.text
    assert missing_field in r.json()["error"]
    assert await _invoice_count(db) == 0
    assert await _next_number(auth_client) == before_next


async def test_blank_required_details_field_is_rejected(auth_client, valid_invoice_payload):
    """Whitespace-only is as unusable as absent on a tax document."""
    valid_invoice_payload["details"]["shipment_id"] = "   "
    r = await auth_client.post("/invoice/save", json=valid_invoice_payload)
    assert r.status_code == 400, r.text
    assert "shipment_id" in r.json()["error"]


@pytest.mark.parametrize("body", ["not json at all", "", "[1,2,3]", '"a string"', "42"])
async def test_non_object_bodies_are_rejected_with_400_not_500(auth_client, body):
    """These raised inside request.json() and surfaced as an opaque 500."""
    r = await auth_client.post(
        "/invoice/save", content=body, headers={"Content-Type": "application/json"}
    )
    assert r.status_code == 400, f"body {body!r} -> {r.status_code}: {r.text[:200]}"
    assert "error" in r.json()


# ─── The happy path still works ──────────────────────────────────────────────

async def test_valid_invoice_is_saved_with_correct_totals(auth_client, db, valid_invoice_payload):
    """10 x ₹100 + 4 x ₹60 = ₹1240 taxable, 5% IGST = ₹62, total ₹1302."""
    r = await auth_client.post("/invoice/save", json=valid_invoice_payload)
    assert r.status_code == 200, r.text

    body = r.json()
    assert body["invoice_no"] == "ST/26-27/028"  # empty table -> LAST_KNOWN_INVOICE + 1

    inv = (await db.execute(select(Invoice))).scalars().one()
    assert inv.total_qty == 14
    assert float(inv.total_taxable) == pytest.approx(1240.0)
    assert float(inv.total_igst) == pytest.approx(62.0)
    assert float(inv.total_amount) == pytest.approx(1302.0)
    assert inv.shipment_id == "FBA15TEST001"
    assert inv.fc_code == "ISK3"
    assert inv.recipient_state == "Maharashtra"


async def test_number_advances_by_exactly_one_per_saved_invoice(
    auth_client, db, valid_invoice_payload
):
    """The core sequence invariant: no gaps, no repeats."""
    saved = []
    for i in range(3):
        payload = dict(valid_invoice_payload)
        payload["details"] = {**valid_invoice_payload["details"], "shipment_id": f"FBA{i:08d}"}
        r = await auth_client.post("/invoice/save", json=payload)
        assert r.status_code == 200, r.text
        saved.append(r.json()["invoice_no"])

    assert saved == ["ST/26-27/028", "ST/26-27/029", "ST/26-27/030"]

    numbers = sorted(
        n for (n,) in await db.execute(select(Invoice.invoice_number))
    )
    assert numbers == [28, 29, 30], "sequence has a gap or a duplicate"


async def test_rejected_invoice_between_two_valid_ones_leaves_no_gap(
    auth_client, valid_invoice_payload
):
    """A failed attempt must not consume the number the next invoice needs."""
    first = await auth_client.post("/invoice/save", json=valid_invoice_payload)
    assert first.json()["invoice_no"] == "ST/26-27/028"

    rejected = await auth_client.post("/invoice/save", json={})
    assert rejected.status_code == 400

    payload = dict(valid_invoice_payload)
    payload["details"] = {**valid_invoice_payload["details"], "shipment_id": "FBA15TEST002"}
    second = await auth_client.post("/invoice/save", json=payload)
    assert second.json()["invoice_no"] == "ST/26-27/029", (
        "the rejected attempt consumed a number — ISSUE-002 has regressed"
    )


# ─── Duplicate handling ──────────────────────────────────────────────────────

async def test_duplicate_invoice_no_returns_409_not_500(auth_client, valid_invoice_payload):
    """invoice_no is UNIQUE; a collision used to raise IntegrityError."""
    first = await auth_client.post("/invoice/save", json=valid_invoice_payload)
    assert first.status_code == 200
    existing = first.json()["invoice_no"]

    clash = dict(valid_invoice_payload)
    clash["details"] = {
        **valid_invoice_payload["details"],
        "shipment_id": "FBA15TEST999",
        "invoice_no": existing,
    }
    r = await auth_client.post("/invoice/save", json=clash)

    assert r.status_code == 409, f"expected 409, got {r.status_code}: {r.text[:200]}"
    assert existing in r.json()["error"]


async def test_user_supplied_invoice_number_is_honoured(auth_client, db, valid_invoice_payload):
    """Operators can override the number; the sequence must follow it."""
    valid_invoice_payload["details"]["invoice_no"] = "ST/26-27/077"
    r = await auth_client.post("/invoice/save", json=valid_invoice_payload)
    assert r.status_code == 200, r.text
    assert r.json()["invoice_no"] == "ST/26-27/077"

    inv = (await db.execute(select(Invoice))).scalars().one()
    assert inv.invoice_number == 77

    # And the next auto-generated number continues from there, not from 28.
    assert await _next_number(auth_client) == "ST/26-27/078"


# ─── Defensive coercion ──────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "quantity,rate,expected_qty,expected_taxable",
    [
        ("10", "100", 10, 1000.0),   # strings from an HTML form
        (10, 100, 10, 1000.0),        # ints
        (10.0, 100.5, 10, 1005.0),    # floats
        ("oops", 100, 0, 0.0),        # unparseable -> 0, must not 500
        (None, 100, 0, 0.0),          # null -> 0, must not 500
    ],
)
async def test_numeric_fields_are_coerced_without_500(
    auth_client, db, valid_invoice_payload, quantity, rate, expected_qty, expected_taxable
):
    """These reached `qty * rate` directly and raised TypeError on bad input."""
    valid_invoice_payload["items"] = [
        {"description": "probe", "quantity": quantity, "rate": rate, "gst_rate": 5}
    ]
    r = await auth_client.post("/invoice/save", json=valid_invoice_payload)
    assert r.status_code == 200, f"qty={quantity!r} rate={rate!r} -> {r.text[:200]}"

    inv = (await db.execute(select(Invoice))).scalars().one()
    assert inv.total_qty == expected_qty
    assert float(inv.total_taxable) == pytest.approx(expected_taxable)


async def test_non_dict_items_are_skipped_not_fatal(auth_client, valid_invoice_payload):
    """A malformed entry inside an otherwise valid list must not 500."""
    valid_invoice_payload["items"] = [
        "garbage",
        {"description": "real", "quantity": 2, "rate": 50, "gst_rate": 5},
    ]
    r = await auth_client.post("/invoice/save", json=valid_invoice_payload)
    assert r.status_code == 200, r.text


async def test_missing_supplier_falls_back_to_default_gstin(
    auth_client, db, valid_invoice_payload
):
    """data.get('supplier', {}) broke when the key was present but null."""
    from app.invoice.company_data import SUPPLIER_GSTIN

    valid_invoice_payload["supplier"] = None
    valid_invoice_payload["recipient"] = None
    r = await auth_client.post("/invoice/save", json=valid_invoice_payload)
    assert r.status_code == 200, r.text

    inv = (await db.execute(select(Invoice))).scalars().one()
    assert inv.supplier_gstin == SUPPLIER_GSTIN


# ─── Auth ────────────────────────────────────────────────────────────────────

async def test_invoice_save_requires_auth(client, valid_invoice_payload):
    r = await client.post("/invoice/save", json=valid_invoice_payload)
    assert r.status_code == 303
    assert r.headers["location"] == "/login"
