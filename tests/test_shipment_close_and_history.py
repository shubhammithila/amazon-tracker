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
