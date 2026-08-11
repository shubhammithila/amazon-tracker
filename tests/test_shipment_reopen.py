"""Reopening a locked packing day so the warehouse can fix a miscount.

Asked for: *"even after the day is verified or submitted. give option for the warehouse
team to edit the units and carton and then resubmit."*

Before this, `save_packing` refused a verified or shipped day with a 409 reading "Ask
the owner to reopen it" — and **no reopen route existed anywhere in the app**. The
packing screen's banner said the same thing. So the advice pointed at nothing, and the
only real recovery from a miscount was to edit the database by hand.

Two decisions shape every test here:

* **Ops may reopen, not just the owner.** A miscount is discovered on the floor by the
  person who did the counting, and the correction is the same manual work as the
  original entry.
* **An invoiced day is refused.** `/invoice/save` has already spent a number from a
  legally-sequential GST series against those exact quantities, so changing them would
  leave the tax document and the packing record disagreeing, with nothing in the app
  able to notice afterwards. That is the one case the warehouse cannot fix quietly.

Reopening also **clears the owner's verification**. His approval is what gates a GST
invoice, so it has to refer to the numbers actually on the day — otherwise his sign-off
would silently cover figures he never saw.
"""
import pytest

from app.shipment import logic

pytestmark = pytest.mark.regression

ASIN = "B0AAA00001"
SECOND = "B0AAA00002"
MONDAY = "2026-07-30"


async def _packed(ops_client, entries=None, cartons=30, date=MONDAY):
    """Enter units and cartons for a day, as ops."""
    r = await ops_client.post(
        f"/shipment/packing/{date}",
        json={"entries": entries or [{"asin": ASIN, "units": 100}], "cartons": cartons},
    )
    assert r.status_code == 200, r.text


async def _submitted(ops_client, **kw):
    date = kw.pop("date", MONDAY)
    await _packed(ops_client, date=date, **kw)
    r = await ops_client.post(f"/shipment/packing/{date}/submit")
    assert r.status_code == 200, r.text
    return r.json()


async def _verified(auth_client, ops_client, **kw):
    date = kw.pop("date", MONDAY)
    await _submitted(ops_client, date=date, **kw)
    r = await auth_client.post(f"/shipment/packing/{date}/verify")
    assert r.status_code == 200, r.text
    return r.json()


async def _day(auth_client, date=MONDAY):
    body = (await auth_client.get("/shipment/active")).json()
    return next((d for d in body["days"] if d["pack_date"] == date), None)


# ─── The loop the request asked for ──────────────────────────────────────────

async def test_the_warehouse_can_reopen_edit_and_resubmit(auth_client, ops_client, plan_factory):
    """The whole requirement, end to end, as ops — not the owner.

    100 units were verified; the real count was 120. Ops reopens, corrects it, submits
    again, and the day carries the corrected number.
    """
    await plan_factory()
    await _verified(auth_client, ops_client)

    r = await ops_client.post(f"/shipment/packing/{MONDAY}/reopen")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == logic.STATUS_OPEN

    # The edit that was impossible before.
    r = await ops_client.post(
        f"/shipment/packing/{MONDAY}",
        json={"entries": [{"asin": ASIN, "units": 120}], "cartons": 33},
    )
    assert r.status_code == 200, r.text

    r = await ops_client.post(f"/shipment/packing/{MONDAY}/submit")
    assert r.status_code == 200, r.text

    day = await _day(auth_client)
    assert day["total_units"] == 120, "the corrected units were not stored"
    assert day["total_cartons"] == 33, "the corrected carton count was not stored"
    assert day["status"] == logic.STATUS_SUBMITTED


async def test_reopening_withdraws_the_owners_verification(auth_client, ops_client, plan_factory):
    """The audit trail must not claim he approved numbers he never saw.

    Verification is what allows a GST invoice to be raised, so a day that has been
    edited since has to be verified again — and `verified_at` must not survive as a
    timestamp against different figures.
    """
    await plan_factory()
    await _verified(auth_client, ops_client)
    assert (await _day(auth_client))["verified_at"] is not None

    await ops_client.post(f"/shipment/packing/{MONDAY}/reopen")

    day = await _day(auth_client)
    assert day["status"] == logic.STATUS_OPEN
    assert day["verified_at"] is None, (
        "the verification timestamp survived the reopen, so the record says the owner "
        "approved a day that has been edited since"
    )
    assert day["submitted_at"] is None, (
        "the submission timestamp survived, so the day claims to be submitted while "
        "being open for editing"
    )


async def test_a_reopened_day_cannot_reach_an_invoice_until_verified_again(
    auth_client, ops_client, plan_factory
):
    """The consequence that makes the point of clearing verification.

    A reopened day is `open`, and `/invoice-payload` accepts only `verified` — so the
    corrected numbers cannot be billed until the owner has actually seen them.
    """
    await plan_factory()
    await _verified(auth_client, ops_client)
    await ops_client.post(f"/shipment/packing/{MONDAY}/reopen")

    r = await auth_client.post(
        "/shipment/invoice-payload", json={"pack_dates": [MONDAY]}
    )
    assert r.status_code == 400
    assert "not verified" in r.json()["error"].lower()

    # And after re-verifying, it can.
    await ops_client.post(f"/shipment/packing/{MONDAY}/submit")
    await auth_client.post(f"/shipment/packing/{MONDAY}/verify")
    r = await auth_client.post(
        "/shipment/invoice-payload", json={"pack_dates": [MONDAY]}
    )
    assert r.status_code == 200, r.text


async def test_a_held_day_can_be_reopened(auth_client, ops_client, plan_factory):
    """Held is below the threshold, not approved — and a miscount is often exactly
    why a day looks too small to ship."""
    await plan_factory()
    # 10 units in 2 cartons: below both 25/500, so held.
    await _submitted(ops_client, entries=[{"asin": ASIN, "units": 10}], cartons=2)
    assert (await _day(auth_client))["status"] == logic.STATUS_HELD

    r = await ops_client.post(f"/shipment/packing/{MONDAY}/reopen")
    assert r.status_code == 200, r.text
    assert (await _day(auth_client))["status"] == logic.STATUS_OPEN


async def test_a_submitted_day_can_be_reopened(auth_client, ops_client, plan_factory):
    """"even after the day is verified or submitted" — both were named."""
    await plan_factory()
    await _submitted(ops_client)
    r = await ops_client.post(f"/shipment/packing/{MONDAY}/reopen")
    assert r.status_code == 200, r.text
    assert (await _day(auth_client))["status"] == logic.STATUS_OPEN


# ─── The GST guard: the one case the warehouse may not fix ───────────────────

async def test_an_invoiced_day_is_refused_and_the_invoice_is_named(
    auth_client, ops_client, plan_factory, db
):
    """The dangerous case, and the reason this is not simply "unlock everything".

    A number from the legally-sequential GST series has been spent against these exact
    quantities. Editing them here would leave the invoice and the packing record
    disagreeing, and nothing in the app could detect it afterwards — the double-invoice
    guard fires on `invoice_id`, not on the numbers.

    The error names the invoice, because "already on invoice ST/26-27/028" is what lets
    the owner go and look it up; "cannot reopen" would send him hunting.
    """
    from app.models import Invoice, ShipmentPackingDay
    from sqlalchemy import select

    await plan_factory()
    await _verified(auth_client, ops_client)

    invoice = Invoice(invoice_no="ST/26-27/028", invoice_number=28, shipment_id="FBA1")
    db.add(invoice)
    await db.commit()
    await db.refresh(invoice)

    day = (await db.execute(
        select(ShipmentPackingDay).where(ShipmentPackingDay.pack_date == MONDAY)
    )).scalar_one()
    day.invoice_id = invoice.id
    day.status = logic.STATUS_SHIPPED
    await db.commit()

    r = await ops_client.post(f"/shipment/packing/{MONDAY}/reopen")
    assert r.status_code == 409, r.text
    error = r.json()["error"]
    assert "ST/26-27/028" in error, (
        f"the invoice is not named, so the owner cannot look it up: {error}"
    )
    assert r.json()["invoice_number"] == "ST/26-27/028"

    # And the day is untouched — a refusal must not half-apply.
    await db.refresh(day)
    assert day.status == logic.STATUS_SHIPPED
    assert day.invoice_id == invoice.id


async def test_the_invoiced_day_stays_uneditable_after_the_refusal(
    auth_client, ops_client, plan_factory, db
):
    """The guard would be worthless if the save path had been opened up instead.

    `save_packing` must still refuse the day, or reopening would merely be a
    formality that ops could skip.
    """
    from app.models import Invoice, ShipmentPackingDay
    from sqlalchemy import select

    await plan_factory()
    await _verified(auth_client, ops_client)

    invoice = Invoice(invoice_no="ST/26-27/029", invoice_number=29, shipment_id="FBA2")
    db.add(invoice)
    await db.commit()
    await db.refresh(invoice)
    day = (await db.execute(
        select(ShipmentPackingDay).where(ShipmentPackingDay.pack_date == MONDAY)
    )).scalar_one()
    day.invoice_id = invoice.id
    await db.commit()

    r = await ops_client.post(
        f"/shipment/packing/{MONDAY}",
        json={"entries": [{"asin": ASIN, "units": 999}], "cartons": 99},
    )
    assert r.status_code == 409, "an invoiced day accepted an edit without a reopen"
    assert (await _day(auth_client))["total_units"] == 100, "the units were changed"


# ─── Refusals that are about clarity rather than safety ──────────────────────

async def test_an_already_open_day_is_a_409(auth_client, ops_client, plan_factory):
    """Nothing to reopen. Said plainly rather than answering 200 to a no-op, which
    would have the screen report success for an action it did not take."""
    await plan_factory()
    await _packed(ops_client)          # saved, never submitted → open
    r = await ops_client.post(f"/shipment/packing/{MONDAY}/reopen")
    assert r.status_code == 409
    assert "already open" in r.json()["error"].lower()


async def test_a_day_that_was_never_packed_is_a_404(auth_client, ops_client, plan_factory):
    await plan_factory()
    r = await ops_client.post("/shipment/packing/2026-08-15/reopen")
    assert r.status_code == 404


async def test_a_bad_date_is_rejected(auth_client, ops_client, plan_factory):
    await plan_factory()
    r = await ops_client.post("/shipment/packing/not-a-date/reopen")
    assert r.status_code == 400


async def test_reopen_needs_no_active_plan_to_fail_safely(ops_client):
    """No plan at all: a 404, not a 500. The packing screen calls this from a banner,
    and an unhandled error there reads as a broken app."""
    r = await ops_client.post(f"/shipment/packing/{MONDAY}/reopen")
    assert r.status_code == 404


# ─── The screen ──────────────────────────────────────────────────────────────

def _ops_source() -> str:
    from pathlib import Path

    return (
        Path(__file__).resolve().parent.parent / "templates" / "ops.html"
    ).read_text(encoding="utf-8")


def test_the_packing_screen_offers_the_reopen_button():
    """The banner used to give advice — "ask the owner to reopen the day" — for an
    action that did not exist. It has to be a control.

    Comments are stripped first: this file's own note explaining what the old wording
    was would otherwise match the check for that wording. Exactly what
    ``tests/test_ops_ui.py::_without_comments`` exists for.
    """
    from test_ops_ui import _without_comments

    source = _ops_source()
    assert "reopenDay" in source, "the packing screen has no way to reopen a day"
    assert "/reopen" in source, "nothing calls the reopen endpoint"
    assert "ask the owner to reopen" not in _without_comments(source), (
        "the screen still tells the packer to ask the owner, which was advice with no "
        "action behind it"
    )


def test_the_reopen_is_confirmed_before_it_withdraws_verification():
    """It un-verifies a finished day, so a mis-click has a cost: the owner has to
    approve it again before it can be invoiced."""
    source = _ops_source()
    start = source.index("async function reopenDay")
    body = source[start:start + 1200]
    assert "confirm(" in body, (
        "reopening is not confirmed, so a mis-tap silently withdraws the owner's "
        "approval of a finished day"
    )
    assert "verif" in body.lower(), (
        "the confirmation does not mention that the day must be verified again"
    )
