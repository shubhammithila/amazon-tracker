"""The owner APPROVES an over-pack once, and both screens go quiet.

Reported as *"error on shipments page and the same on orders page. give option to hide this error.
abhi ye kya tha ki extra packing kar diya to theek hai na. puch hi ke kiya. give admin right to
remove this error by his page and warehouse page also."*

**The banner it silences is not wrong, and that is the whole constraint.**
``POST /shipment/invoice-payload`` bills ``logic.units_by_asin`` — the PACKED units — so 62 boxed
against a plan of 60 really does put 62 on a GST invoice. So this is an APPROVAL rather than a hide
button: a blanket dismiss would also silence the next overage, which might be a 200-unit miscount
heading for a tax document.

Two properties carry the whole design, and each has a test named for it:

1. **The stored figure is the packed TOTAL, never the excess.** Comparing against
   ``max(planned, approved)`` means raising the plan SUPERSEDES the approval; comparing against
   ``planned + approved`` would compound with it.
2. **An approval is a CEILING, not a mute.** Approving 400 and then packing 100 more must warn on
   the new 100 — the assertion a boolean approval would pass while being wrong.

Every test here fails against the code before this change.
"""
import inspect

import pytest

from app.shipment import logic, repository

pytestmark = pytest.mark.regression

# From conftest.plan_factory. B0BBB00001 is 'jau sattu' 1kg, planned 200.
ASIN = "B0BBB00001"
PLANNED = 200
MONDAY = "2026-07-30"
TUESDAY = "2026-07-31"


async def _pack(client, pack_date, units, asin=ASIN, cartons=30):
    return await client.post(
        f"/shipment/packing/{pack_date}",
        json={"entries": [{"asin": asin, "units": units}], "cartons": cartons},
    )


async def _approve(client, plan_id, packed, asin=ASIN, approved=True):
    return await client.post(
        f"/shipment/plan/{plan_id}/items/approve-over-pack",
        json={"asins": [asin], "approved": approved, "packed": {asin: packed}},
    )


def _row(body, asin=ASIN):
    return next(i for i in body["items"] if i["asin"] == asin)


# ── The arithmetic ───────────────────────────────────────────────────────────


def test_an_approved_over_pack_is_no_longer_reported():
    """The point of the feature: the decision was taken, so the error stops."""
    assert logic.unapproved_over_pack(60, 62, 62) == 0


def test_an_approval_is_a_CEILING_and_not_a_mute():
    """**Approving 62 approves 62, not the row for ever.**

    A plan runs about a week across several packing days, so "approve Monday, pack more on
    Thursday" is the normal shape of the data rather than an edge case. A boolean approval would
    answer 0 here and leave a 200-unit miscount reaching a GST invoice with nothing on screen.
    """
    assert logic.unapproved_over_pack(60, 80, 62) == 18


def test_with_no_approval_it_is_exactly_the_raw_over_pack():
    """Nothing about an unapproved row changes, so the existing banner is untouched."""
    assert logic.unapproved_over_pack(60, 62, None) == logic.over_packed(60, 62) == 2
    # An empty string is what a blank form field sends; it must read as "no approval" rather than
    # raising or silently becoming 0-approved.
    assert logic.unapproved_over_pack(60, 62, "") == 2


def test_a_RAISED_PLAN_supersedes_the_approval_rather_than_adding_to_it():
    """**This is why the stored figure is the TOTAL and the comparison is `max`.**

    Store the excess (+2) and compare against ``planned + approved``, and the owner raising the
    plan 60 -> 100 for a bigger truck silently moves the threshold to 102: two units approved
    against a 60-unit plan have followed the row onto a 100-unit plan and authorised an overage
    nobody looked at. ``max`` self-corrects in the safe direction.

    The second assertion is the one that fails if anyone stores the excess — it would answer 3.
    """
    assert logic.unapproved_over_pack(100, 62, 62) == 0
    assert logic.unapproved_over_pack(100, 105, 62) == 5


def test_over_packed_itself_still_takes_exactly_two_arguments():
    """The approval is a SIBLING function, not a third parameter.

    ``over_packed`` keeps stating the raw reconciliation between the plan and reality, with
    nothing that can silence it — the same reason ``remaining_for`` refuses an ``available``
    argument. Asserted on the signature, because a default-valued third parameter would pass
    every value-based test while no caller passed it.
    """
    params = list(inspect.signature(logic.over_packed).parameters)
    assert params == ["planned", "packed"], (
        "over_packed grew a parameter; the approval belongs in unapproved_over_pack"
    )


# ── The route ────────────────────────────────────────────────────────────────


async def test_approving_clears_the_owners_banner_and_records_the_figure(
    auth_client, ops_client, plan_factory
):
    """The reported case, end to end on the owner's payload."""
    plan = await plan_factory()
    await _pack(ops_client, MONDAY, PLANNED * 2)

    before = _row((await auth_client.get("/shipment/active")).json())
    assert before["over_packed"] == PLANNED, "nothing to approve; the fixture is wrong"

    r = await _approve(auth_client, plan.id, PLANNED * 2)
    assert r.status_code == 200, r.text
    assert r.json()["count"] == 1

    after = _row((await auth_client.get("/shipment/active")).json())
    assert after["over_packed"] == 0, "the banner would still be showing"
    assert after["over_pack_approved_units"] == PLANNED * 2, (
        "the approval is invisible on the row, so there is no way to revoke it"
    )
    assert after["over_pack_approved_at"], "no date, so the row cannot say when"


async def test_packing_MORE_after_an_approval_warns_again(
    auth_client, ops_client, plan_factory
):
    """**The single most important assertion in this file.**

    A boolean or timestamp approval returns 0 here and passes every other test, while a later
    100-unit discrepancy reaches a GST invoice with both screens silent.
    """
    plan = await plan_factory()
    await _pack(ops_client, MONDAY, PLANNED * 2)
    await _approve(auth_client, plan.id, PLANNED * 2)

    # A second day, so this is the real "approve Monday, pack more Thursday" sequence.
    await _pack(ops_client, TUESDAY, 100)

    row = _row((await auth_client.get("/shipment/active")).json())
    assert row["over_packed"] == 100, (
        "approving 400 silenced a later 100-unit over-pack, so the approval is a mute rather "
        "than a ceiling"
    )
    assert row["over_pack_approved_units"] == PLANNED * 2, "the earlier decision was lost"


async def test_revoking_brings_the_whole_excess_back(
    auth_client, ops_client, plan_factory
):
    """Reversible, like ``excluded_at``: a mis-click is one click back."""
    plan = await plan_factory()
    await _pack(ops_client, MONDAY, PLANNED * 2)
    await _approve(auth_client, plan.id, PLANNED * 2)

    r = await _approve(auth_client, plan.id, None, approved=False)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "revoked"

    row = _row((await auth_client.get("/shipment/active")).json())
    assert row["over_packed"] == PLANNED
    assert row["over_pack_approved_units"] is None, (
        "absence must be the single representation of 'no decision'"
    )
    assert row["over_pack_approved_at"] is None


async def test_the_packer_cannot_approve_his_own_over_pack(
    ops_client, auth_client, plan_factory
):
    """Admin-only. The warehouse reports the count; signing it off is the owner's call, because
    the surplus reaches a GST invoice and only he can decide to bill it."""
    plan = await plan_factory()
    await _pack(ops_client, MONDAY, PLANNED * 2)

    r = await _approve(ops_client, plan.id, PLANNED * 2)
    assert r.status_code == 403, r.text

    row = _row((await auth_client.get("/shipment/active")).json())
    assert row["over_packed"] == PLANNED, "an ops approval was stored"


async def test_a_stale_packed_figure_is_REFUSED_and_names_both_numbers(
    auth_client, ops_client, plan_factory
):
    """**Approving is a decision about a specific quantity.**

    The owner clicks approve on the 400 he can see; if the packer saved 100 more while the page
    sat open, storing 500 approves more than he ever looked at and storing 400 approves a figure
    that no longer exists. Refused, with both numbers named, rather than resolved either way —
    the same reason ``/ads/apply`` re-reads the live bid before writing.
    """
    plan = await plan_factory()
    await _pack(ops_client, MONDAY, PLANNED * 2)
    await _pack(ops_client, TUESDAY, 100)          # the drift: 500 packed now

    r = await _approve(auth_client, plan.id, PLANNED * 2)   # still approving 400
    assert r.status_code == 409, r.text
    body = r.json()
    assert "400" in body["error"] and "500" in body["error"], (
        "the refusal does not name what was shown and what is packed now"
    )

    row = _row((await auth_client.get("/shipment/active")).json())
    assert row["over_pack_approved_units"] is None, "a stale figure was stored anyway"


async def test_an_approval_without_a_packed_figure_is_refused(
    auth_client, ops_client, plan_factory
):
    """A missing figure must not be read as "approve whatever is current" — that is the drift
    case with the check skipped."""
    plan = await plan_factory()
    await _pack(ops_client, MONDAY, PLANNED * 2)

    r = await auth_client.post(
        f"/shipment/plan/{plan.id}/items/approve-over-pack",
        json={"asins": [ASIN], "approved": True},
    )
    assert r.status_code == 400, r.text

    row = _row((await auth_client.get("/shipment/active")).json())
    assert row["over_pack_approved_units"] is None


async def test_an_excluded_row_cannot_be_approved(
    auth_client, ops_client, plan_factory, db
):
    """It is not in the plan, so there is nothing to approve — and a stored decision would
    reappear if the row were ever restored."""
    plan = await plan_factory()
    await repository.set_item_excluded(db, plan.id, [ASIN], True)

    changed = await repository.set_over_pack_approved(db, plan.id, {ASIN: PLANNED * 2})
    assert changed == [], "an excluded row was approved"


# ── The warehouse screen ─────────────────────────────────────────────────────


async def test_the_packing_payload_carries_the_approved_THRESHOLD(
    auth_client, ops_client, plan_factory
):
    """**The number, never a pre-computed excess.**

    An excess computed on the server is a function of a packed total the packer is at that moment
    changing, so it would be stale on his first keystroke — which is exactly what the live
    recompute exists to avoid. The screen folds this into its own comparison.
    """
    plan = await plan_factory()
    await _pack(ops_client, MONDAY, PLANNED * 2)
    await _approve(auth_client, plan.id, PLANNED * 2)

    body = (await ops_client.get(f"/shipment/packing/{TUESDAY}")).json()
    row = _row(body)
    assert row["over_pack_approved"] == PLANNED * 2
    assert row["over_packed"] == 0, (
        "the packing payload still reports the approved excess, so it disagrees with the "
        "owner's payload about the same fact"
    )


def test_both_screens_net_the_approval_through_ONE_helper():
    """Source-level, because no runtime test here drives a browser.

    The ops banner and the row's own ``+N over`` tag are two computations of one rule, and the
    codebase has shipped a server contract passing while the client did something else four times
    (the pause feature, ``intakeFromShipment``, ``renderInvoiceBar``, the from_stock split).
    """
    from pathlib import Path

    source = Path("templates/ops.html").read_text(encoding="utf-8")
    assert "function unapprovedOver(" in source, (
        "the excess is computed inline in two places, which is how the row tag comes to "
        "disagree with the banner above it"
    )
    body = source[source.index("function unapprovedOver(") :]
    body = body[: body.index("\nfunction ", 1)]
    assert "over_pack_approved" in body, "the live check ignores the owner's approval"
    assert "Math.max(" in body and "+ Number(row.over_pack_approved" not in body, (
        "the threshold ADDS the approval to the plan instead of taking the larger, so a raised "
        "plan would compound with it"
    )
    # **THREE readers, and the third is why this assertion counts them by name.**
    # Found by opening the page: the banner had gone quiet while every row still showed "+2 over",
    # because `renderRows` held its own copy of the arithmetic for the FIRST render — the one
    # `markOverPack` never touches, since that only runs on a keystroke. An earlier version of
    # this test counted call sites of the helper and passed at 3 while the third reader was a
    # duplicate. Named readers, so a fourth cannot appear silently.
    for reader in ("renderRows", "markOverPack", "renderMessages"):
        start = source.index(f"function {reader}(")
        scope = source[start : source.index("\n}\n", start) + 3]
        assert "unapprovedOver(" in scope, (
            f"{reader} computes the over-pack itself instead of using the shared helper, so it "
            "can disagree with the other two about the same row"
        )
    # And the subtraction itself lives in exactly ONE place. `unapprovedOver` is the only function
    # allowed to take `planned` away from a packed total; anywhere else is a fourth copy waiting to
    # drift, which is precisely how the first-render bug survived.
    helper = source[source.index("function unapprovedOver(") :]
    helper = helper[: helper.index("\n}\n") + 3]
    outside = source.replace(helper, "")
    assert "- Number(row.planned" not in outside and "- Number(i.planned" not in outside, (
        "the plan is subtracted from a packed total outside unapprovedOver, so that copy will "
        "not honour an approval"
    )


def test_the_live_recompute_still_does_not_rebuild_the_row():
    """Pinned again here because this change edits that function: re-rendering would replace the
    input under the caret and drop the number mid-type."""
    from pathlib import Path

    source = Path("templates/ops.html").read_text(encoding="utf-8")
    # Scoped by the function's own closing brace at column 0, not by "the next `function`" — the
    # next top-level declaration here is an addEventListener, so searching for `function` runs
    # into an unrelated comment block and asserts nothing about markOverPack at all.
    start = source.index("function markOverPack(")
    body = source[start : source.index("\n}\n", start) + 3]
    assert "renderRows" not in body, "markOverPack re-renders, so typing loses the caret"
    assert "classList.toggle" in body
    assert "unapprovedOver(row)" in body, "the row tag ignores the owner's approval"


# ── The owner's screen ───────────────────────────────────────────────────────


def test_the_dashboard_offers_the_approval_and_a_way_to_revoke_it():
    """An approval the owner cannot see is invisible state with no way out — the ``available``
    column defect, which was editable for a whole build and fed nothing."""
    from pathlib import Path

    source = Path("templates/shipment.html").read_text(encoding="utf-8")
    assert 'id="approve-over"' in source, "there is no way to approve an over-pack"
    assert 'id="revoke-over"' in source, "an approval cannot be undone"
    assert "over_pack_approved_units" in source, "the approved figure is never shown"
    assert "setOverPackApproved" in source

    # The packed figure must travel, and it must be the DISPLAYED one. Anchored to the ASSIGNMENT,
    # not to the substring `Number(i.packed || 0)`: that expression also appears in the confirm
    # dialog a few lines above, so a mutation sending a hardcoded 0 left it present and the first
    # version of this assertion passed. Sixth instance of that trap in this codebase.
    body = source[source.index("async function setOverPackApproved(") :]
    body = body[: body.index("\n}\n", 1) + 3]
    assert "packed[i.asin] = Number(i.packed || 0)" in body, (
        "the screen does not send the packed figure it rendered, so either drift goes undetected "
        "or the guard fires on every approval and the button looks broken"
    )


async def test_an_approval_does_NOT_survive_a_plan_close(
    auth_client, ops_client, plan_factory, db
):
    """**Documents today's behaviour rather than asserting it away.**

    Closing a plan carries packed-but-unshipped days onto the next one and inserts fresh
    ``To-Ship-0`` rows for ASINs the new plan lacks. Those are NEW ``ShipmentPlanItem`` rows, so the
    approval does not travel: the carried units sit against a plan row of 0 and the banner returns
    on the carrier plan. The owner re-approves after a close.

    Deliberately not fixed here. Copying the approval inside ``close_plan`` is a second write path
    into the function whose own history is the worst incident in this codebase —
    ``delete_draft_plans`` destroying 400 units of packed stock under a docstring asserting it was
    safe. If a future reader decides to carry it, this test is where they will find out that today
    it does not, instead of discovering it from the owner.
    """
    plan = await plan_factory()
    await _pack(ops_client, MONDAY, PLANNED * 2)
    await auth_client.post(f"/shipment/packing/{MONDAY}/submit")
    await _approve(auth_client, plan.id, PLANNED * 2)

    closed = await auth_client.post(f"/shipment/plan/{plan.id}/close")
    assert closed.status_code == 200, closed.text

    target_id = closed.json()["target_plan_id"]
    assert target_id, "the close carried nothing, so there is no carrier plan to inspect"
    items = await repository.load_plan_items(db, target_id)
    row = next((i for i in items if i.asin == ASIN), None)
    assert row is not None, "the carried day's ASIN has no row on the new plan"
    assert row.over_pack_approved_units is None, (
        "the approval now survives a close — which may be an improvement, but it changes what "
        "this test documents and CLAUDE.md's 'known limit' note needs updating with it"
    )


def test_the_approval_is_not_in_the_editable_item_fields():
    """``saveItems()`` posts every dirty row's plan fields from client state that may be minutes
    old. An approval in that whitelist would mean editing an unrelated SKU field silently
    re-approves a row from a stale figure."""
    assert "over_pack_approved_units" not in repository.EDITABLE_ITEM_FIELDS
    assert "over_pack_approved_at" not in repository.EDITABLE_ITEM_FIELDS


# ── What must NOT change ─────────────────────────────────────────────────────
#
# The approval reaches no quantity. `units_by_asin` bills the GST invoice and
# `verified_units_by_asin` declares what Amazon should expect — both read PACKED units, both are
# already correct, and neither may ever take the approval into account. Capping a declared
# quantity here would make Amazon expect a different count from what arrives at the FC.


async def test_the_amazon_upload_still_declares_what_was_PACKED(
    auth_client, ops_client, plan_factory
):
    """Asserted on the CELL, not on the code being unchanged. This is the FC guard."""
    from io import BytesIO

    from openpyxl import load_workbook

    plan = await plan_factory()
    await _pack(ops_client, MONDAY, PLANNED * 2)
    await auth_client.post(f"/shipment/packing/{MONDAY}/submit")
    await auth_client.post(f"/shipment/packing/{MONDAY}/verify")
    await _approve(auth_client, plan.id, PLANNED * 2)

    r = await auth_client.get(
        "/shipment/download/shipment-file.xlsx"
        f"?mode=verified&pack_dates={MONDAY}"
    )
    assert r.status_code == 200, r.text
    sheet = load_workbook(BytesIO(r.content)).active
    quantities = {
        row[1]: row[4] for row in sheet.iter_rows(min_row=2, values_only=True) if row[1]
    }
    assert quantities.get(ASIN) == PLANNED * 2, (
        "the approval reached the Amazon upload quantity, so Amazon expects a different count "
        "from what arrives at the FC"
    )


async def test_the_invoice_still_bills_what_was_PACKED(
    auth_client, ops_client, plan_factory
):
    """The approval acknowledges the discrepancy; it does not resolve it. Billing the planned
    quantity "now that it is approved" would understate a tax document."""
    plan = await plan_factory()
    await _pack(ops_client, MONDAY, PLANNED * 2)
    await auth_client.post(f"/shipment/packing/{MONDAY}/submit")
    await auth_client.post(f"/shipment/packing/{MONDAY}/verify")
    await _approve(auth_client, plan.id, PLANNED * 2)

    r = await auth_client.post(
        "/shipment/invoice-payload", json={"pack_dates": [MONDAY]}
    )
    assert r.status_code == 200, r.text
    line = next(
        item for item in r.json()["items"] if item.get("asin") == ASIN
    )
    assert line["quantity"] == PLANNED * 2, (
        "the approval changed the GST invoice quantity"
    )


async def test_the_packed_sheet_gains_no_approval_column(
    auth_client, ops_client, plan_factory
):
    """Screen-only, as asked. ``_totals_row`` sums every trailing column with ``int(...)``, so an
    "Approved" column would total meaninglessly — the trap that forced ``build_portfolio_xlsx``
    to be its own function."""
    from io import BytesIO

    from openpyxl import load_workbook

    from app.shipment.documents import IDENTITY_HEADERS

    plan = await plan_factory()
    await _pack(ops_client, MONDAY, PLANNED * 2)
    await _approve(auth_client, plan.id, PLANNED * 2)

    r = await auth_client.get(
        f"/shipment/download/packed.xlsx?date_from={MONDAY}&date_to={MONDAY}"
    )
    assert r.status_code == 200, r.text
    sheet = load_workbook(BytesIO(r.content)).active
    headers = [c for c in next(sheet.iter_rows(values_only=True)) if c]
    assert headers == list(IDENTITY_HEADERS) + ["Units", "Made today", "From stock"], (
        f"the packed sheet's columns changed: {headers}"
    )


# ── The Orders tab is a DISMISSAL, not an approval ───────────────────────────


def test_the_orders_warning_is_dismissed_for_the_DAY_and_stores_nothing():
    """Deliberately a different mechanism, because it is a different kind of warning.

    It compares packed units against TODAY'S ORDERS, reaches no invoice and no plan, and
    SELF-CLEARS — two more orders arriving makes it disappear with nobody doing anything. The
    whole row expires at midnight (`order_packed_entries` is UNIQUE on `(pack_date, asin)`), so a
    stored approval would outlive the thing it was about.

    Keyed on the DATE so tomorrow's genuine over-pack is not hidden by today's shrug.
    """
    from pathlib import Path

    source = Path("templates/orders.html").read_text(encoding="utf-8")
    assert 'id="dismiss-over"' in source, "there is no way to dismiss the warning"
    assert "sessionStorage" in source, "the dismissal is not remembered at all"

    body = source[source.index("function overDismissed(") :]
    body = body[: body.index("\nfunction dismissOver(")]
    assert "data.pack_date" in body, (
        "the dismissal is not keyed on the day, so it would hide tomorrow's warning too"
    )
    assert "localStorage" not in body, (
        "a dismissal surviving into next week is indistinguishable from a broken warning"
    )
    # And no column was added for it.
    from app import models

    assert not hasattr(models.OrderPackedEntry, "over_pack_approved_units"), (
        "the Orders tab grew a column for a warning that expires nightly"
    )
