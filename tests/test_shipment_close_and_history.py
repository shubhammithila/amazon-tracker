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
