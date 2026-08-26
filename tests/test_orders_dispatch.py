"""Today's dispatch: what the warehouse packs, and what it has packed.

The numbers in these tests are the ones measured on the live account on 2026-08-25, because
the rule they encode was derived from that measurement rather than from the documentation:

    ship_by 2026-08-25  Shipped    PickedUp          200
    ship_by 2026-08-25  Shipped    PendingPickUp      64
    ship_by 2026-08-25  Pending    (none)              3
    ship_by 2026-08-26+ Unshipped  PendingSchedule   128

**264 orders is the day's dispatch, and it does not shrink when the courier arrives.** The
previous screen keyed on "awaiting pickup", which drained 264 -> 64 overnight as Amazon
flipped orders to `PickedUp` — taking the day's packed tally with it. That regression is what
this file exists to prevent.
"""
from datetime import date, datetime

import pytest

from app.orders import logic, repository

pytestmark = pytest.mark.regression

TODAY = date(2026, 8, 25)

#: A catalogue with two sizes of one product, a heavy single-size product, and one product
#: whose pack weight is unknown. Asymmetric on purpose: 3 x 5 kg outweighs 3 x 0.5 kg, so a
#: quantity-ordered sort and a weight-ordered sort give different answers here.
CATALOGUE = {
    "B0ABC500": {"name": "ABC Sattu", "weight": 0.5, "brand": "Mithila Foods"},
    "B0ABC1KG": {"name": "ABC Sattu", "weight": 1.0, "brand": "Mithila Foods"},
    "B0RICE5KG": {"name": "Usna Chawal", "weight": 5.0, "brand": "Howrah Foods"},
    "B0NOWEIGHT": {"name": "Mystery Mix", "weight": 0, "brand": "Mithila Foods"},
}


def _order(order_id, items, **overrides):
    """An order shaped as `repository.load_orders` returns one, dispatched today by default."""
    base = {
        "amazon_order_id": order_id,
        "status": "Shipped",
        "easyship_status": "PendingPickUp",
        "ship_service_level": "Std IN EZ National COD",
        "purchase_date_utc": datetime(2026, 8, 24, 6, 0),
        # 18:29Z == 23:59 IST on the 25th: exactly what every real order carries.
        "latest_ship_date_utc": datetime(2026, 8, 25, 18, 29),
        "city": "NAVSARI",
        "state": "GUJARAT",
        "items": items,
    }
    base.update(overrides)
    return base


def _item(asin, quantity=1, sku=None):
    return {"asin": asin, "quantity_ordered": quantity,
            "seller_sku": sku or f"sku-{asin}", "title": f"title {asin}"}


# ─── The rule: ship-by today AND labelled ────────────────────────────────────


@pytest.mark.parametrize("status,easyship,expected,why", [
    ("Shipped", "PendingPickUp", True, "labelled, waiting for the courier — the 64"),
    ("Shipped", "PickedUp", True, "collected today; the tally must survive the courier"),
    ("Shipped", "OutForDelivery", True, "on the van, still today's dispatch"),
    ("Shipped", "Delivered", True, "arrived same-day; it was still packed today"),
    ("Shipped", "ReturningToSeller", True, "it went out today, so it was packed today"),
    ("Unshipped", "PendingSchedule", False, "no label — the ecom team's job, the 128"),
    ("Pending", None, False, "payment unconfirmed, nothing to pack"),
    ("Canceled", "LabelCanceled", False, "the label was withdrawn; there is no parcel"),
])
def test_only_labelled_orders_due_today_are_dispatch(status, easyship, expected, why):
    """The exact split measured on the account, asserted status by status.

    `PickedUp` being INCLUDED is the point of the whole redesign: 200 of 264 orders were in
    that state by morning, and a rule that dropped them would empty the screen mid-shift.
    """
    order = _order("403-1", [_item("B0ABC500")], status=status, easyship_status=easyship)
    assert logic.is_todays_dispatch(order, TODAY) is expected, why


def test_an_order_due_tomorrow_is_not_todays_dispatch():
    """The 128 `Unshipped` orders are due 26 Aug onward — tomorrow's problem, and ecom's."""
    order = _order("403-1", [_item("B0ABC500")],
                   latest_ship_date_utc=datetime(2026, 8, 26, 18, 29))
    assert logic.is_todays_dispatch(order, TODAY) is False


def test_the_1995_sentinel_is_never_todays_dispatch():
    """Amazon sends 1995-01-01 when there is no Easy Ship ship-by.

    Treated as a real date it is simply not today, which is the right answer — but asserted
    because a `<=` comparison instead of `==` would sweep every sentinel order onto the sheet.
    """
    order = _order("403-1", [_item("B0ABC500")],
                   latest_ship_date_utc=datetime(1995, 1, 1, 0, 0))
    assert logic.is_todays_dispatch(order, TODAY) is False


def test_bucket_for_is_not_disturbed_by_the_dispatch_rule():
    """The two questions stay separate, and this is the guard on that.

    `bucket_for` assigns ONE bucket per order and the picking sheet, its four section totals
    and its Excel export all read it. Today's dispatch deliberately CROSSES two of those
    buckets, so it had to be a new predicate — if someone later "simplifies" by folding the
    dispatch rule into the bucketing, the owner's screen changes silently.
    """
    labelled = _order("403-1", [_item("B0ABC500")], easyship_status="PendingPickUp")
    collected = _order("403-2", [_item("B0ABC500")], easyship_status="PickedUp")

    assert logic.bucket_for(labelled, TODAY) == logic.BUCKET_PICKUP
    assert logic.bucket_for(collected, TODAY) == logic.BUCKET_DONE
    # Both are today's dispatch even though they are in different buckets. That is exactly
    # the overlap that makes a separate predicate necessary.
    assert logic.is_todays_dispatch(labelled, TODAY) is True
    assert logic.is_todays_dispatch(collected, TODAY) is True


# ─── The rollup: parent product, pack sizes beneath, heaviest first ──────────


def test_parents_are_sorted_by_weight_not_quantity():
    """Weight is what fills the vehicle, so the heaviest product leads.

    Deliberately asymmetric: 3 units of 5 kg rice (15 kg) against 6 units of 500 g sattu
    (3 kg). A quantity sort would put sattu first, so this distinguishes the two rules
    rather than passing under either.
    """
    orders = [
        _order("403-1", [_item("B0ABC500", 6)]),
        _order("403-2", [_item("B0RICE5KG", 3)]),
    ]
    sheet = logic.dispatch_sheet(orders, CATALOGUE, TODAY)
    assert [p["product"] for p in sheet["parents"]] == ["Usna Chawal", "ABC Sattu"]
    assert sheet["parents"][0]["kg"] == pytest.approx(15.0)
    assert sheet["parents"][1]["kg"] == pytest.approx(3.0)


def test_sizes_nest_under_one_parent_and_never_collapse():
    """500 g and 1 kg are different shelves, but "how much ABC Sattu today" is the question.

    So they roll up to one parent AND stay as separate rows beneath it. Collapsing them would
    send the packer to one bin for a pick that needs two.
    """
    orders = [_order("403-1", [_item("B0ABC500", 2), _item("B0ABC1KG", 3)])]
    sheet = logic.dispatch_sheet(orders, CATALOGUE, TODAY)

    assert len(sheet["parents"]) == 1, "two sizes produced two parent rows"
    parent = sheet["parents"][0]
    assert parent["units"] == 5
    assert parent["kg"] == pytest.approx(4.0)          # 2x0.5 + 3x1.0
    assert [s["weight_label"] for s in parent["sizes"]] == ["1 kg", "500g"], (
        "sizes are not ordered heaviest first"
    )


def test_the_kilogram_total_multiplies_pack_size_by_quantity():
    """The number the courier cares about, and it is NET.

    24 x 500g and 12 x 1kg are both 12 kg, so equal weights could not tell a correct total
    from one that summed pack sizes without multiplying. Here the answer is 24.0 where a
    sum-without-multiply gives 1.5.
    """
    orders = [_order(f"403-{i}", [_item("B0ABC500", 1)]) for i in range(24)]
    orders += [_order(f"404-{i}", [_item("B0ABC1KG", 1)]) for i in range(12)]
    sheet = logic.dispatch_sheet(orders, CATALOGUE, TODAY)
    assert sheet["totals"]["kg"] == pytest.approx(24.0)
    assert sheet["totals"]["units"] == 36
    assert sheet["totals"]["orders"] == 36


def test_a_size_with_no_pack_weight_is_counted_in_units_but_not_in_kilograms():
    """Treating an unknown weight as 0 makes a 47 kg sheet quietly report 40.

    The units still appear, because the parcel still has to be packed, and the omission is
    reported so the owner can fix the sheet rather than wonder about the total.
    """
    orders = [
        _order("403-1", [_item("B0ABC500", 2)]),        # 1.0 kg
        _order("403-2", [_item("B0NOWEIGHT", 5)]),      # no pack weight
    ]
    sheet = logic.dispatch_sheet(orders, CATALOGUE, TODAY)
    assert sheet["totals"]["kg"] == pytest.approx(1.0), "an unweighed line polluted the total"
    assert sheet["totals"]["units"] == 7, "the unweighed units must still be packed"
    assert sheet["totals"]["sizes_without_weight"] == 1
    mystery = next(p for p in sheet["parents"] if p["product"] == "Mystery Mix")
    assert mystery["sizes"][0]["kg"] is None, "an unknown weight was reported as a number"


def test_an_unknown_asin_is_shown_and_named_not_dropped():
    """A missing row is stock nobody packs, found when Amazon reports a late shipment."""
    orders = [_order("403-1", [_item("B0MYSTERY1", 4, sku="odd-sku")])]
    sheet = logic.dispatch_sheet(orders, CATALOGUE, TODAY)
    assert "B0MYSTERY1" in sheet["unknown_asins"]
    parent = sheet["parents"][0]
    assert parent["units"] == 4
    assert parent["known"] is False


def test_orders_counts_orders_not_lines():
    """Two units of one SKU in one order is quantity 2, orders 1.

    That distinction is what makes "24 units across 22 orders" mean something: it is the
    parcel count as well as the pick count.
    """
    orders = [
        _order("403-1", [_item("B0ABC500", 2)]),
        _order("403-2", [_item("B0ABC500", 1)]),
    ]
    sheet = logic.dispatch_sheet(orders, CATALOGUE, TODAY)
    size = sheet["parents"][0]["sizes"][0]
    assert size["units"] == 3
    assert size["orders"] == 2, "ORDERS must count orders, not lines"


def test_the_order_list_follows_the_same_parent_order_as_the_summary():
    """Both halves of the printed sheet must read together.

    Two independent sorts would put the summary in weight order and the order list in some
    other order, so a packer working down the page would jump between products.
    """
    orders = [
        _order("403-light", [_item("B0ABC500", 1)]),
        _order("403-heavy", [_item("B0RICE5KG", 3)]),
    ]
    sheet = logic.dispatch_sheet(orders, CATALOGUE, TODAY)
    assert [p["product"] for p in sheet["parents"]][0] == "Usna Chawal"
    assert sheet["orders"][0]["amazon_order_id"] == "403-heavy", (
        "the order list is not grouped in the summary's order"
    )


def test_the_ecom_teams_orders_never_reach_the_dispatch_sheet():
    """The whole point of point 2: the floor sees only its own work."""
    orders = [
        _order("403-mine", [_item("B0ABC500", 1)]),
        _order("403-ecom", [_item("B0ABC500", 99)],
               status="Unshipped", easyship_status="PendingSchedule",
               latest_ship_date_utc=datetime(2026, 8, 26, 18, 29)),
    ]
    sheet = logic.dispatch_sheet(orders, CATALOGUE, TODAY)
    assert sheet["totals"]["units"] == 1, "an unshipped order leaked onto the dispatch sheet"
    assert [o["amazon_order_id"] for o in sheet["orders"]] == ["403-mine"]


# ─── Packed counts ───────────────────────────────────────────────────────────


def test_packed_counts_reach_the_line_and_the_remainder():
    """What the sheet is for: how much of today's work is done."""
    orders = [_order("403-1", [_item("B0ABC500", 10)])]
    sheet = logic.dispatch_sheet(orders, CATALOGUE, TODAY, packed={"B0ABC500": 4})
    size = sheet["parents"][0]["sizes"][0]
    assert size["packed"] == 4
    assert size["remaining"] == 6
    assert sheet["totals"]["packed"] == 4
    assert sheet["totals"]["remaining"] == 6


def test_over_packing_is_reported_and_never_negative():
    """`remaining` clamps at 0 because it reaches a printed sheet; the excess is separate.

    Packed 12 against 10 ordered reads "0 left" AND "+2 over". Collapsing those into "-2
    left" puts a negative quantity on a picking sheet, and dropping the excess entirely hides
    that two parcels will ship — and be invoiced — beyond what today's orders need.
    """
    orders = [_order("403-1", [_item("B0ABC500", 10)])]
    sheet = logic.dispatch_sheet(orders, CATALOGUE, TODAY, packed={"B0ABC500": 12})
    size = sheet["parents"][0]["sizes"][0]
    assert size["remaining"] == 0, "a negative quantity reached the sheet"
    assert size["over_packed"] == 2
    assert sheet["totals"]["over_packed"] == 2


async def test_a_repeated_save_updates_one_row(db, db_schema):
    """The UNIQUE index makes it possible; the upsert makes it happen.

    A warehouse phone re-sends. Inserting each arrival would double the packed count, which
    is the same failure the (day_id, asin) index prevents on the shipment side.
    """
    await repository.save_packed(db, "2026-08-25", [{"asin": "B0ABC500", "units": 5}])
    packed = await repository.save_packed(db, "2026-08-25", [{"asin": "B0ABC500", "units": 8}])
    assert packed == {"B0ABC500": 8}, "a repeated save did not update in place"


async def test_zeroing_a_count_removes_it(db, db_schema):
    """Correcting a mistyped row should remove it, not leave a 0 that reads as "counted"."""
    await repository.save_packed(db, "2026-08-25", [{"asin": "B0ABC500", "units": 5}])
    packed = await repository.save_packed(db, "2026-08-25", [{"asin": "B0ABC500", "units": 0}])
    assert packed == {}


async def test_counts_are_kept_per_day(db, db_schema):
    """"What did we pack on Monday" has to stay answerable.

    This is why the row is keyed on a date rather than on the current order set — the set
    empties itself as Amazon marks orders picked up.
    """
    await repository.save_packed(db, "2026-08-24", [{"asin": "B0ABC500", "units": 3}])
    await repository.save_packed(db, "2026-08-25", [{"asin": "B0ABC500", "units": 9}])
    assert await repository.load_packed(db, "2026-08-24") == {"B0ABC500": 3}
    assert await repository.load_packed(db, "2026-08-25") == {"B0ABC500": 9}


async def test_a_packed_count_survives_the_order_being_collected(db, db_schema):
    """**The regression this whole feature was rebuilt around.**

    Measured: 200 of 264 orders flipped from `PendingPickUp` to `PickedUp` overnight. The old
    screen keyed on "awaiting pickup", so the line the count belonged to vanished and the
    day's tally went with it. Here the order changes status and the sheet keeps both the line
    and the number.
    """
    packed = await repository.save_packed(
        db, "2026-08-25", [{"asin": "B0ABC500", "units": 6}]
    )
    collected = _order("403-1", [_item("B0ABC500", 10)], easyship_status="PickedUp")
    sheet = logic.dispatch_sheet([collected], CATALOGUE, TODAY, packed=packed)

    assert sheet["totals"]["orders"] == 1, "a collected order fell off the dispatch sheet"
    assert sheet["parents"][0]["sizes"][0]["packed"] == 6, (
        "the packed count was lost when the courier collected the order"
    )


# ─── Raw stock: standing, per parent product ─────────────────────────────────


def test_the_raw_stock_table_is_keyed_on_the_product_and_has_no_date():
    """Raw material on a shelf does not vanish at midnight.

    `order_packed_entries` is keyed on (pack_date, asin) because a packed count belongs to a
    day. Raw stock is the opposite: it is a standing quantity, and a `pack_date` would make it
    blank every morning — so tab 1 would read "buy everything" at 9am daily until 33 numbers
    were retyped.

    Keyed on the parent product NAME, not an ASIN, because raw material is bulk: there is no
    such thing as 500 g-flavoured raw sattu.
    """
    from app.models import ProductRawStock

    columns = ProductRawStock.__table__.c
    assert "product" in columns
    assert "raw_kg" in columns
    assert "pack_date" not in columns, (
        "raw stock must NOT be per-day; a dated field is blank every morning and the "
        "purchasing tab would demand re-entry before it meant anything"
    )
    # The unique index is the real guarantee that a repeated save updates one row.
    indexes = {index.name: index for index in ProductRawStock.__table__.indexes}
    assert "idx_product_raw_stock_product" in indexes
    assert indexes["idx_product_raw_stock_product"].unique is True
