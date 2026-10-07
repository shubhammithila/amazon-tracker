"""customer_order_lines: one row per shipped order line, upserted, never doubled."""
from datetime import date

import pytest

from app.repeat import repository

pytestmark = pytest.mark.regression


def _line(order="171-1", item="I1", buyer="k1", day="2026-09-01", sku="1kg cs FBA",
          child="B0CHILD001", parent="B0PARENT01", units=1):
    return {"amazon_order_id": order, "shipment_item_id": item, "buyer_key": buyer,
            "purchase_day": day, "seller_sku": sku, "child_asin": child,
            "parent_asin": parent, "units": units}


async def test_saving_the_same_line_twice_keeps_one_row_with_the_newer_values(db):
    await repository.save_lines(db, [_line(units=1)])
    await repository.save_lines(db, [_line(units=3)])
    rows = await repository.load_lines(db, since="2026-01-01")
    assert len(rows) == 1 and rows[0]["units"] == 3


async def test_two_items_of_one_order_are_two_rows(db):
    await repository.save_lines(db, [_line(item="I1"), _line(item="I2", child="B0CHILD002")])
    assert len(await repository.load_lines(db, since="2026-01-01")) == 2


async def test_load_returns_a_DATE_and_skips_unresolved_lines(db):
    await repository.save_lines(db, [_line(), _line(order="171-2", parent=None, child=None)])
    rows = await repository.load_lines(db, since="2026-01-01")
    assert [r["day"] for r in rows] == [date(2026, 9, 1)]


async def test_purge_is_scoped_to_days_before_the_cutoff(db):
    await repository.save_lines(db, [_line(order="a", day="2025-01-01"),
                                     _line(order="b", day="2026-09-01")])
    assert await repository.purge(db, keep_from="2026-01-01") == 1
    rows = await repository.load_lines(db, since="2000-01-01")
    assert [r["amazon_order_id"] for r in rows] == ["b"]


async def test_resolve_missing_fills_only_null_rows(db):
    await repository.save_lines(db, [
        _line(order="a", child=None, parent=None),
        _line(order="b", sku="other FBA", child="B0KEEP0001", parent="B0KEEPPAR1")])
    n = await repository.resolve_missing(db, {"1kg cs FBA": ("B0CHILD001", "B0PARENT01"),
                                             "other FBA": ("B0WRONG001", "B0WRONG001")})
    assert n == 1
    rows = {r["amazon_order_id"]: r for r in await repository.load_lines(db, since="2000-01-01")}
    assert rows["a"]["parent_asin"] == "B0PARENT01"
    assert rows["b"]["parent_asin"] == "B0KEEPPAR1"


async def test_every_line_is_fba_until_easy_ship_gains_a_customer_key(db):
    from sqlalchemy import select

    from app.models import CustomerOrderLine
    await repository.save_lines(db, [_line()])
    assert (await db.execute(select(CustomerOrderLine.channel))).scalars().all() == ["fba"]


def test_no_column_could_hold_an_email_name_phone_or_address():
    from app.models import CustomerOrderLine
    cols = {c.name for c in CustomerOrderLine.__table__.columns}
    banned = ("email", "name", "phone", "address", "postal", "pin")
    assert not [c for c in cols if any(b in c for b in banned)]


async def test_sku_map_takes_amazons_pairing_and_the_sheet_fills_gaps(db):
    from app.models import EconomicsDaily
    db.add_all([
        EconomicsDaily(day="2026-09-01", child_asin="B0CHILD001", seller_sku="", parent_asin="B0PARENT01"),
        EconomicsDaily(day="2026-09-01", child_asin="B0CHILD001", seller_sku="1kg cs FBA", parent_asin="B0PARENT01"),
    ])
    await db.commit()
    mapping = await repository.sku_map(db, {
        "B0CHILD001": {"fba_sku": "1kg cs FBA"},
        "B0CHILD009": {"fba_sku": "new FBA"},
    })
    assert mapping["1kg cs FBA"] == ("B0CHILD001", "B0PARENT01")
    assert mapping["new FBA"] == ("B0CHILD009", "B0CHILD009")
