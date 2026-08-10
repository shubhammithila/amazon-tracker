"""Packing more than the plan asked for, and why silence about it costs money.

The owner found it in testing: *"in beetroot sattu 1 kg. plan was 50. but Ops team
packed 100. It should show warning to them also. and in the shipment dashboard
warning to me also."*

Nothing had gone wrong in the code — that is what makes it worth a file. Every
number was correct and the situation was still invisible, because
``logic.remaining_for`` clamps at 0. It is *right* to clamp: that figure goes on the
packer's printed morning sheet and into the Amazon upload quantity, where "-50 to
pack" is not a quantity. But the consequence is that

    planned 50, packed 50   -> 0 left to pack
    planned 50, packed 100  -> 0 left to pack

read identically on both screens. A finished row and a doubled one looked the same.

**Why it matters beyond tidiness.** ``POST /shipment/invoice-payload`` aggregates
what was PACKED, not what was planned. So the surplus boxes go to Amazon and appear
on a GST invoice at the packed quantity. The error is discovered at reconciliation,
against a document with a legally-sequential number on it.

So the excess is reported as its own number, ``over_packed``, on both payloads. It
is a warning and never a block: the boxes physically exist, and refusing the entry
would leave the packer unable to record real stock. Only the owner can resolve it,
and only two ways — raise To Ship to match, or have the surplus unpacked.
"""
import pytest

from app.shipment import repository

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


def _row(body, asin=ASIN):
    return next(i for i in body["items"] if i["asin"] == asin)


# ─── The owner's dashboard ───────────────────────────────────────────────────

async def test_the_owner_is_told_when_more_was_packed_than_planned(
    auth_client, ops_client, plan_factory
):
    """The reported case, in the owner's payload."""
    await plan_factory()
    r = await _pack(ops_client, MONDAY, PLANNED * 2)
    assert r.status_code == 200, r.text

    row = _row((await auth_client.get("/shipment/active")).json())
    assert row["over_packed"] == PLANNED, (
        "the owner's payload does not report the excess, so a doubled row looks "
        "exactly like a finished one"
    )


async def test_a_finished_row_is_not_reported_as_over_packed(
    auth_client, ops_client, plan_factory
):
    """The distinction the whole feature rests on. Exactly on plan is not over."""
    await plan_factory()
    await _pack(ops_client, MONDAY, PLANNED)

    row = _row((await auth_client.get("/shipment/active")).json())
    assert row["remaining"] == 0
    assert row["over_packed"] == 0, "a row packed exactly to plan is flagged as over"


async def test_the_number_still_to_pack_is_never_negative(
    auth_client, ops_client, plan_factory
):
    """`remaining` must keep clamping, because it is a printed quantity.

    It reaches the morning PDF and the Amazon upload column. A negative there is not
    a quantity, which is exactly why the excess is a separate field rather than a
    sign change on this one.
    """
    await plan_factory()
    await _pack(ops_client, MONDAY, PLANNED * 3)

    row = _row((await auth_client.get("/shipment/active")).json())
    assert row["remaining"] == 0, f"a negative to-pack figure escaped: {row['remaining']}"
    assert row["over_packed"] == PLANNED * 2


async def test_an_excess_accumulated_over_two_days_is_reported_in_full(
    auth_client, ops_client, plan_factory
):
    """Neither day is over on its own. Together they are.

    The realistic shape of the mistake: a plausible count on Monday, the same
    plausible count again on Tuesday because the sheet still said 200.
    """
    await plan_factory()
    await _pack(ops_client, MONDAY, 150)
    await _pack(ops_client, TUESDAY, 150)

    row = _row((await auth_client.get("/shipment/active")).json())
    assert row["packed"] == 300
    assert row["over_packed"] == 100, (
        "the excess is being judged per day rather than against the plan total"
    )


async def test_only_the_over_packed_rows_are_flagged(
    auth_client, ops_client, plan_factory
):
    """The guard must be narrow, or the banner cries wolf on a normal week."""
    await plan_factory()
    await _pack(ops_client, MONDAY, PLANNED * 2)

    body = (await auth_client.get("/shipment/active")).json()
    flagged = [i["asin"] for i in body["items"] if i["over_packed"] > 0]
    assert flagged == [ASIN], f"rows with no over-pack were flagged too: {flagged}"


async def test_raising_the_plan_clears_the_warning(auth_client, ops_client, plan_factory):
    """One of the two ways out, and it must actually work.

    A warning that cannot be resolved reads as the app being broken. The other way
    out — unpacking — is the packer correcting his entry downwards, covered below.
    """
    plan = await plan_factory()
    await _pack(ops_client, MONDAY, PLANNED * 2)
    assert _row((await auth_client.get("/shipment/active")).json())["over_packed"] > 0

    r = await auth_client.post(
        "/shipment/items",
        json={"plan_id": plan.id, "items": [{"asin": ASIN, "shipment_plan": PLANNED * 2}]},
    )
    assert r.status_code == 200, r.text

    row = _row((await auth_client.get("/shipment/active")).json())
    assert row["over_packed"] == 0, "raising To Ship to match the boxes did not clear it"
    assert row["remaining"] == 0


async def test_correcting_the_count_down_clears_the_warning(
    auth_client, ops_client, plan_factory
):
    """The other way out: the packer mistyped and fixes it."""
    await plan_factory()
    await _pack(ops_client, MONDAY, PLANNED * 2)
    assert _row((await auth_client.get("/shipment/active")).json())["over_packed"] > 0

    await _pack(ops_client, MONDAY, PLANNED)
    row = _row((await auth_client.get("/shipment/active")).json())
    assert row["over_packed"] == 0, "correcting the count did not clear the warning"


# ─── The packer's screen ────────────────────────────────────────────────────

async def test_the_packer_is_told_too(ops_client, plan_factory):
    """"It should show warning to them also."

    Him first, in fact: he is the one who can still stop, and he is the one holding
    the boxes. The owner finds out later.
    """
    await plan_factory()
    await _pack(ops_client, MONDAY, PLANNED * 2)

    body = (await ops_client.get(f"/shipment/packing/{MONDAY}")).json()
    row = _row(body)
    assert row["over_packed"] == PLANNED, (
        "the packer's payload does not report the excess, so the screen cannot warn "
        "him before he boxes more of it"
    )


async def test_the_packers_warning_counts_the_units_he_is_entering_now(
    ops_client, plan_factory
):
    """A subtle one, and the reason the two payloads compute it differently.

    ``remaining`` on the packing screen deliberately EXCLUDES the current day, so the
    target does not appear to move as he types. ``over_packed`` must not: he needs to
    be warned about the total that will exist, which includes today.
    """
    await plan_factory()
    await _pack(ops_client, MONDAY, PLANNED * 2)

    row = _row((await ops_client.get(f"/shipment/packing/{MONDAY}")).json())
    assert row["packed_before"] == 0, (
        "premise changed: packed_before should exclude the day being viewed"
    )
    assert row["over_packed"] == PLANNED, (
        "the excess ignores the units entered on this very day, so the packer would "
        "see no warning about the count he just typed"
    )


async def test_an_earlier_days_excess_still_shows_on_a_later_day(
    ops_client, plan_factory
):
    """He opens Tuesday. Monday's over-pack must still be visible.

    Otherwise the warning disappears overnight and he tops the row up again.
    """
    await plan_factory()
    await _pack(ops_client, MONDAY, PLANNED * 2)

    row = _row((await ops_client.get(f"/shipment/packing/{TUESDAY}")).json())
    assert row["packed_before"] == PLANNED * 2
    assert row["over_packed"] == PLANNED


# ─── It is a warning, not a block ───────────────────────────────────────────

async def test_over_packing_is_never_refused(
    ops_client, plan_factory, read_committed
):
    """The boxes exist. Refusing the entry would leave real stock unrecorded.

    Asserted at the database, not just on the status code: a route that returned 200
    and quietly dropped the surplus would be worse than a refusal, because the packer
    would believe it saved.
    """
    plan = await plan_factory()
    r = await _pack(ops_client, MONDAY, PLANNED * 2)
    assert r.status_code == 200, r.text

    day = await read_committed(repository.get_day, plan.id, MONDAY)
    entries = await read_committed(repository.load_entries, day.id)
    stored = next(e for e in entries if e.asin == ASIN)
    assert stored.units == PLANNED * 2, (
        "the surplus units were silently discarded — the packer would think they saved"
    )


async def test_an_over_packed_day_can_still_be_submitted_and_verified(
    auth_client, ops_client, plan_factory
):
    """The day's work is real and must be closable.

    Blocking the submit would strand a day of counting behind a warning the packer
    cannot resolve — only the owner can.
    """
    await plan_factory()
    await _pack(ops_client, MONDAY, PLANNED * 2)

    r = await ops_client.post(f"/shipment/packing/{MONDAY}/submit")
    assert r.status_code == 200, r.text
    r = await auth_client.post(f"/shipment/packing/{MONDAY}/verify")
    assert r.status_code == 200, r.text


async def test_the_invoice_bills_what_was_packed(
    auth_client, ops_client, plan_factory
):
    """Why the warning exists at all, made explicit.

    The bridge aggregates PACKED units, so the surplus reaches a GST invoice. This
    test is not asserting a wish — it pins the actual behaviour that makes silence
    expensive, so nobody "fixes" the warning by capping the invoice at the plan
    instead. Capping would be worse: the boxes ship and part of the shipment would
    then be billed to nobody.
    """
    await plan_factory()
    await _pack(ops_client, MONDAY, PLANNED * 2)
    await ops_client.post(f"/shipment/packing/{MONDAY}/submit")
    await auth_client.post(f"/shipment/packing/{MONDAY}/verify")

    body = (
        await auth_client.post(
            "/shipment/invoice-payload", json={"pack_dates": [MONDAY]}
        )
    ).json()
    line = next(i for i in body["items"] if i["asin"] == ASIN)
    assert line["quantity"] == PLANNED * 2, (
        "the invoice quantity no longer follows what was packed. If this was "
        "deliberate, note that the surplus boxes still ship — capping the invoice at "
        "the plan bills nobody for them."
    )


# ─── Excluded rows must not nag ──────────────────────────────────────────────

async def test_an_excluded_row_is_not_reported_as_over_packed(
    auth_client, ops_client, plan_factory
):
    """A removed row should not produce a warning the owner cannot act on.

    Note the exclude itself is refused for a packed row (that is the GST guard in
    tests/test_shipment_exclusion.py), so this uses a row packed on a DIFFERENT ASIN
    to set the plan to 0 — the move that guard actually recommends.
    """
    plan = await plan_factory()
    # To Ship = 0 with nothing packed, then exclude: the supported path.
    r = await auth_client.post(
        "/shipment/plan/%d/items/exclude" % plan.id,
        json={"asins": ["B0CCC00001"], "excluded": True},
    )
    assert r.status_code == 200, r.text

    body = (await auth_client.get("/shipment/active")).json()
    assert "B0CCC00001" not in [i["asin"] for i in body["items"]], (
        "an excluded row is still on the owner's active view"
    )
