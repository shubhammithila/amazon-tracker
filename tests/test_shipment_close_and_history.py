"""Closing a shipment plan, carrying its unshipped days, and reading it back.

Asked for: *"lets give a button to close the shipment plan and if there is some
uninvoiced or unverified units we can carry forward it to the next shipment plan and
we can give a button to view recent or old shipment plan history."*

The live case: 10-18 Aug shipped and invoiced, then 19 Aug packed 400 units in 9
cartons and verified — below the 25-carton threshold, so it cannot ship alone. Sales
data has moved and a new plan is wanted. Those 400 units are in boxes and must not be
packed again.

Two properties carry most of the weight:

* **The DAY moves, not its units.** ``logic.remaining_for`` deliberately ignores
  ``available``, so adding carried units there would tell the packer to box them a
  second time. Moving ``plan_id`` needs no new arithmetic because every aggregation
  reaches days through ``load_days(plan_id)``.
* **A closed plan stays readable.** Closing used to make ``attach_invoice`` 404 and
  ``packed.xlsx`` unavailable, which is the reconciliation hole this closes.
"""
import pytest

from app.models import ShipmentPackingDay, ShipmentPlan

pytestmark = pytest.mark.regression


def test_the_carry_and_close_columns_exist_on_the_models():
    """Declared on the models, so `test_migrations_match_models` compares them.

    `carried_from_plan_id` is the lineage: without it a day that moved is
    indistinguishable from one packed against the new plan, and reconciliation cannot
    explain why a plan shows units for a day it never opened.
    """
    assert hasattr(ShipmentPackingDay, "carried_from_plan_id")
    assert hasattr(ShipmentPlan, "closed_at")
    assert ShipmentPackingDay.__table__.c.carried_from_plan_id.nullable is True
    assert ShipmentPlan.__table__.c.closed_at.nullable is True


# ─── The eligibility rule, pure ──────────────────────────────────────────────

from app.shipment import logic


def _day(pack_date, status, units=100, cartons=5, **extra):
    """A day shaped as `load_days_with_entries` returns one."""
    day = {
        "pack_date": pack_date,
        "status": status,
        "total_units": units,
        "total_cartons": cartons,
        "invoice_id": None,
        "inbound_plan_id": None,
        "shipment_confirmation_id": None,
        "entries": [{"asin": "B0AAA00001", "units": units, "note": None}],
    }
    day.update(extra)
    return day


def test_a_held_or_verified_day_with_no_shipment_carries():
    """The live case. 19 Aug is verified, unshipped, uninvoiced — it must move.

    Verified is carriable, not just held: the threshold is not the only reason a day
    sits unshipped, and the owner's approval does not make boxes ship.
    """
    days = [
        _day("2026-08-19", logic.STATUS_VERIFIED, units=400, cartons=9),
        _day("2026-08-20", logic.STATUS_HELD, units=50, cartons=2),
        _day("2026-08-21", logic.STATUS_SUBMITTED, units=80, cartons=3),
    ]
    result = logic.carriable_days(days)
    assert [d["pack_date"] for d in result["carry"]] == [
        "2026-08-19", "2026-08-20", "2026-08-21",
    ]
    assert result["blocked"] == []


def test_a_shipped_day_does_not_carry():
    """Its units are already in the next plan's fba_stock.

    `parse_stock_csv` sums the three afn-inbound columns, so a shipped day is already
    inside `deficit = projection - fba_stock`. Carrying it would count the same units
    twice and inflate every plan after.
    """
    days = [_day("2026-08-18", logic.STATUS_SHIPPED, units=296, cartons=16)]
    result = logic.carriable_days(days)
    assert result["carry"] == []


def test_an_invoiced_day_does_not_carry():
    """A GST number has been spent against those exact quantities."""
    days = [_day("2026-08-17", logic.STATUS_VERIFIED, invoice_id=41)]
    result = logic.carriable_days(days)
    assert result["carry"] == []


def test_a_day_with_a_half_created_amazon_shipment_blocks_the_close():
    """`inbound_plan_id` with no confirmation means a plan exists at Amazon.

    `clear_inbound_plan` scopes its cleanup by (plan_id, inbound_plan_id), so moving
    the day would detach it from the only query that can cancel it — leaving a plan
    Amazon holds and this app can no longer reach.
    """
    days = [_day("2026-08-20", logic.STATUS_VERIFIED, inbound_plan_id="wf123")]
    result = logic.carriable_days(days)
    assert result["carry"] == []
    assert len(result["blocked"]) == 1
    assert result["blocked"][0]["pack_date"] == "2026-08-20"
    assert "amazon" in result["blocked"][0]["reason"].lower()


def test_an_open_day_blocks_the_close():
    """The packer is mid-shift. Moving the day under him loses the count."""
    days = [_day("2026-08-20", logic.STATUS_OPEN)]
    result = logic.carriable_days(days)
    assert result["carry"] == []
    assert len(result["blocked"]) == 1
    assert "open" in result["blocked"][0]["reason"].lower()


def test_an_empty_day_neither_carries_nor_blocks():
    """A day row with no entries is bookkeeping, not boxes. Moving it would put an
    empty date on the new plan and make the history list read as if work happened."""
    days = [_day("2026-08-20", logic.STATUS_HELD, units=0, cartons=0, entries=[])]
    result = logic.carriable_days(days)
    assert result["carry"] == []
    assert result["blocked"] == []


def test_shipped_but_uninvoiced_days_are_reported_separately():
    """They stay on the old plan, but the owner must be told before it closes.

    Nothing carries them — the units are at Amazon. What they need is to stay
    invoice-able, which is the history half of this feature. Silence here is the
    held-days bug again: the boxes leave every screen with no invoice raised.
    """
    days = [
        _day("2026-08-18", logic.STATUS_SHIPPED, invoice_id=None),
        _day("2026-08-17", logic.STATUS_SHIPPED, invoice_id=41),
    ]
    result = logic.carriable_days(days)
    assert [d["pack_date"] for d in result["shipped_uninvoiced"]] == ["2026-08-18"]


def test_a_day_with_no_entries_neither_carries_nor_blocks():
    """An unloaded ORM relationship must not raise inside packed_units.

    The `entries` field is an ORM relationship that may be `None` if not
    eager-loaded. The `or []` guard in line 727 is what makes this safe — without
    it, `packed_units(None)` would raise, blocking every close on a relationship
    loading bug rather than on the day's actual state. A `None` here means the
    query did not JOIN entries, not that the day has zero entries, so it must
    neither carry (unknown units) nor block (no boxes).
    """
    days = [_day("2026-08-20", logic.STATUS_HELD, units=100, entries=None)]
    result = logic.carriable_days(days)
    assert result["carry"] == [], "entries=None must not carry — units are unknown"
    assert result["blocked"] == [], "entries=None must not block — no boxes exist"
    assert result["shipped_uninvoiced"] == []


def test_an_open_day_with_zero_units_does_not_block():
    """An empty open day is bookkeeping, not work in progress.

    The `if units:` guard on line 736 prevents an open day with no entries from
    blocking the close. Without it, a zero-unit row created during a migration or a
    mis-click would block every close until manually deleted, even though there are
    no boxes to protect. Only open days WITH units should block, since only those
    represent a packer mid-shift who might be adding more.
    """
    days = [
        _day("2026-08-20", logic.STATUS_OPEN, units=150, cartons=5),
        _day("2026-08-21", logic.STATUS_OPEN, units=0, cartons=0, entries=[]),
    ]
    result = logic.carriable_days(days)
    assert len(result["blocked"]) == 1, "only the units-bearing open day should block"
    assert result["blocked"][0]["pack_date"] == "2026-08-20"
    assert "open" in result["blocked"][0]["reason"].lower()


def test_an_open_day_with_an_amazon_shipment_blocks_as_open_not_amazon():
    """The open check comes first, and that ordering is deliberate.

    If a day is both open AND has an `inbound_plan_id`, the STATUS_OPEN block
    (line 735) fires first, so the reason reports "open" not "amazon". Submitting
    the day is more immediately actionable than unwinding a half-created Amazon
    shipment, so the block must name the easier fix. Reordering those two checks
    would make this test fail by reporting an Amazon shipment when the real blocker
    is the packer's count.
    """
    days = [_day("2026-08-20", logic.STATUS_OPEN, inbound_plan_id="wf123", units=100)]
    result = logic.carriable_days(days)
    assert len(result["blocked"]) == 1
    reason = result["blocked"][0]["reason"].lower()
    assert "open" in reason, "must name the more actionable fix"
    assert "amazon" not in reason, "must not mention the Amazon shipment"


def test_an_unrecognised_status_fails_safe():
    """A typo or a new status must neither carry nor block.

    The `if status not in CARRIABLE_STATUSES: continue` line (743) is the fail-safe:
    a day whose status is unrecognised (e.g. a typo like "verfiied", or a new status
    added without updating this function) is silently skipped. That is correct:
    carrying a day whose state is unknown could move boxes that should not move, and
    blocking on it would refuse every close with no actionable error. Making the day
    visibly absent (not in carry, not in blocked) is the safe failure mode — the
    owner sees it on the old plan with an unexpected status and can investigate.
    """
    days = [_day("2026-08-20", "verfiied", units=100)]
    result = logic.carriable_days(days)
    assert result["carry"] == [], "unrecognised status must not carry — state unknown"
    assert result["blocked"] == [], "unrecognised status must not block — would refuse all closes"
    assert result["shipped_uninvoiced"] == []


# ─── Carrying a day between plans ────────────────────────────────────────────

from app.shipment import repository


async def test_carrying_a_day_moves_it_and_stamps_its_origin(db, plan_factory):
    """One column update, and the lineage that explains it.

    The day must appear on the new plan with its entries intact, and the old plan must
    say where it went — otherwise a reconciliation of the old plan shows a date that
    simply vanished.
    """
    old = await plan_factory()
    new = await plan_factory(status=repository.STATUS_DRAFT)
    await repository.save_packing_entries(
        db, old.id, "2026-08-19",
        [{"asin": "B0AAA00001", "units": 400}], cartons=9,
    )

    moved = await repository.carry_days_to_plan(db, old.id, new.id, ["2026-08-19"])
    assert moved == ["2026-08-19"]

    old_days = await repository.load_days_with_entries(db, old.id)
    new_days = await repository.load_days_with_entries(db, new.id)
    assert [d["pack_date"] for d in old_days] == []
    assert [d["pack_date"] for d in new_days] == ["2026-08-19"]
    assert new_days[0]["total_units"] == 400
    assert new_days[0]["total_cartons"] == 9, "the carton count did not travel"
    assert new_days[0]["entries"] == [
        {"asin": "B0AAA00001", "units": 400, "note": None}
    ]
    assert new_days[0]["carried_from_plan_id"] == old.id


async def test_a_carried_day_keeps_its_verified_status(db, plan_factory):
    """19 Aug is verified and must stay so, or the invoice it still needs is blocked.

    Re-opening it would also throw away the owner's approval of numbers he did see.
    """
    old = await plan_factory()
    new = await plan_factory(status=repository.STATUS_DRAFT)
    await repository.save_packing_entries(
        db, old.id, "2026-08-19", [{"asin": "B0AAA00001", "units": 400}], cartons=9,
    )
    day = await repository.get_day(db, old.id, "2026-08-19")
    day.status = logic.STATUS_VERIFIED
    await db.commit()

    await repository.carry_days_to_plan(db, old.id, new.id, ["2026-08-19"])

    carried = await repository.get_day(db, new.id, "2026-08-19")
    assert carried is not None
    assert carried.status == logic.STATUS_VERIFIED


async def test_carrying_is_idempotent(db, plan_factory):
    """Closing twice must not move anything twice, or duplicate a date.

    The UNIQUE index on (plan_id, pack_date) would reject the second insert, but the
    second call should simply find nothing to do rather than raise.
    """
    old = await plan_factory()
    new = await plan_factory(status=repository.STATUS_DRAFT)
    await repository.save_packing_entries(
        db, old.id, "2026-08-19", [{"asin": "B0AAA00001", "units": 400}], cartons=9,
    )

    first = await repository.carry_days_to_plan(db, old.id, new.id, ["2026-08-19"])
    second = await repository.carry_days_to_plan(db, old.id, new.id, ["2026-08-19"])
    assert first == ["2026-08-19"]
    assert second == []
    new_days = await repository.load_days_with_entries(db, new.id)
    assert len(new_days) == 1
