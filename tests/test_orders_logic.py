"""The Orders tab: a daily picking sheet from Amazon Easy Ship orders.

Asked for: *"the orders which have to be shipped today. item wise weight wise qty
totalled. total number of orders of each item and total orders."*

So the aggregate IS the feature and the order rows are raw material. Three properties
carry most of the weight, and each was measured against the live account on 2026-08-24
rather than assumed:

* **Easy Ship is identified by `ShipServiceLevel` containing `EZ`**, not by
  `FulfillmentChannel == "MFN"`. Three `S02-…` orders are MFN `"Standard"` with a
  ship-by of **1995-01-01**, a sentinel that would sit at the top of every morning's
  sheet as 31 years overdue.
* **Ship-by dates are IST.** Every real `LatestShipDate` is `18:29Z`, which is
  `23:59 IST` — Amazon means "end of day in India". Bucketed in UTC, tonight's orders
  land on the wrong day.
* **Pack sizes never collapse.** 500 g and 1 kg of one product are separate lines or
  the packer goes to the wrong bin, while the KG column is what the courier needs.
"""
import pytest

from app.models import AmazonOrder, AmazonOrderItem

pytestmark = pytest.mark.regression


def test_the_order_tables_exist_with_utc_named_timestamps():
    """`*_utc` naming is half the timezone guard.

    The app is IST and the API is UTC; a column called `latest_ship_date` invites a
    future reader to render it directly, which shows every deadline 5.5 hours early on
    the one screen whose job is "what must go out today".
    """
    for column in ("purchase_date_utc", "latest_ship_date_utc"):
        assert column in AmazonOrder.__table__.c, f"{column} missing"
    assert "latest_ship_date" not in AmazonOrder.__table__.c, (
        "an un-suffixed timestamp column invites rendering UTC as local time"
    )
    # The order id is the upsert key: a re-refresh must update, never duplicate.
    assert AmazonOrder.__table__.c.amazon_order_id.unique is True
    assert "asin" in AmazonOrderItem.__table__.c
    assert "seller_sku" in AmazonOrderItem.__table__.c


# ─── IST, the EZ filter, and the 1995 sentinel ───────────────────────────────

from datetime import date, datetime, timedelta, timezone

from app.orders import logic


def _order(**overrides):
    """An order shaped as the repository returns one."""
    base = {
        "amazon_order_id": "403-0000000-0000001",
        "status": "Unshipped",
        "easyship_status": "PendingSchedule",
        "ship_service_level": "Std IN EZ National COD",
        "purchase_date_utc": datetime(2026, 8, 24, 6, 0),
        "latest_ship_date_utc": datetime(2026, 8, 24, 18, 29),
        "order_total": 319.0,
        "city": "NAVSARI",
        "state": "GUJARAT",
        "items": [],
    }
    base.update(overrides)
    return base


def test_a_ship_by_deadline_reads_as_end_of_day_in_ist():
    """The real payload: 18:29 UTC is 23:59 IST, not 18:29.

    Every LatestShipDate on this account is 18:29Z — Amazon expressing "end of today in
    India". Rendered as UTC the packer sees a deadline 5.5 hours earlier than the truth,
    on the one screen whose whole purpose is what must go out today.
    """
    utc = datetime(2026, 7, 12, 18, 29, tzinfo=timezone.utc)
    ist = logic.to_ist(utc)
    assert (ist.hour, ist.minute) == (23, 59), f"got {ist:%H:%M}"
    assert ist.date() == date(2026, 7, 12), "the calendar day must not shift"


def test_a_naive_timestamp_is_treated_as_utc():
    """Rows come back from SQLite without a tzinfo.

    SQLAlchemy's DateTime is naive, so the value read from the database has no timezone
    even though it was stored as UTC. Treating it as local would silently subtract 5.5
    hours from every deadline — a fixed offset error, which is the hardest kind to spot
    because everything still looks plausible.
    """
    ist = logic.to_ist(datetime(2026, 7, 12, 18, 29))
    assert (ist.hour, ist.minute) == (23, 59)


def test_to_ist_passes_none_through():
    """A missing timestamp must not raise — a cancelled order can lack a ship-by."""
    assert logic.to_ist(None) is None


@pytest.mark.parametrize("level,expected", [
    ("Std IN EZ National COD", True),
    ("Std IN EZ Remote", True),
    ("Std IN EZ Metro COD", True),
    ("Standard", False),          # the real S02- orders
    ("", False),
    (None, False),
])
def test_easy_ship_is_identified_by_the_service_level(level, expected):
    """`ShipServiceLevel` contains EZ; FulfillmentChannel does not distinguish.

    Both Easy Ship and plain self-ship report MFN, so filtering on the channel lets
    three real `S02-…` "Standard" orders into the sheet — and those carry a ship-by of
    1995-01-01, which would sit at the top of every morning as 31 years overdue.
    """
    assert logic.is_easy_ship(level) is expected


def test_the_1995_sentinel_is_not_a_deadline():
    """Amazon sends 1995-01-01 when there is no Easy Ship ship-by.

    Treated as a real date it sorts before everything and reads as catastrophically
    overdue. Treated as None it is simply absent, which is the truth.
    """
    order = _order(latest_ship_date_utc=datetime(1995, 1, 1, 0, 0))
    assert logic.ship_by_date(order) is None


def test_a_real_deadline_is_the_ist_calendar_date():
    order = _order(latest_ship_date_utc=datetime(2026, 8, 24, 18, 29))
    assert logic.ship_by_date(order) == date(2026, 8, 24)


# ─── The picking sheet: the feature itself ───────────────────────────────────

CATALOGUE = {
    "B0CHANA500": {"name": "Chana Sattu", "weight": 0.5, "brand": "Mithila Foods"},
    "B0CHANA1KG": {"name": "Chana Sattu", "weight": 1.0, "brand": "Mithila Foods"},
    "B0RAGI1KG0": {"name": "Ragi Atta", "weight": 1.0, "brand": "Mithila Foods"},
    "B0POSTA100": {"name": "Bengali Posta", "weight": 0.1, "brand": "Howrah Foods"},
}
TODAY = date(2026, 8, 24)


def _item(asin, qty=1):
    return {"asin": asin, "seller_sku": f"sku-{asin}", "title": f"title {asin}",
            "quantity_ordered": qty}


def _lines(sheet, bucket=logic.BUCKET_TODAY):
    return sheet["sections"][bucket]["lines"]


def _totals(sheet, bucket=logic.BUCKET_TODAY):
    return sheet["sections"][bucket]["totals"]


def test_quantities_and_order_counts_are_aggregated_per_product_and_size():
    """Asked for: item wise, weight wise, qty totalled, total orders of each item.

    QUANTITY sums units; ORDERS counts the orders that contain the line. They differ, and
    both matter: "24 units across 22 orders" tells the packer how much to pick AND how
    many parcels to expect. Counting lines instead of orders would report 24/24 and
    quietly overstate the parcel count.
    """
    orders = [
        _order(amazon_order_id="1", items=[_item("B0CHANA500", 2)]),
        _order(amazon_order_id="2", items=[_item("B0CHANA500", 1)]),
        _order(amazon_order_id="3", items=[_item("B0RAGI1KG0", 1)]),
    ]
    sheet = logic.picking_sheet(orders, CATALOGUE, TODAY)
    chana = next(l for l in _lines(sheet) if l["product"] == "Chana Sattu")
    assert chana["quantity"] == 3, "units did not sum"
    assert chana["orders"] == 2, "ORDERS must count orders, not lines"
    assert _totals(sheet)["orders"] == 3, "the total counts distinct orders"
    assert _totals(sheet)["quantity"] == 4


def test_two_sizes_of_one_product_stay_on_separate_lines():
    """500g and 1kg live on different shelves.

    Collapsing them into "Chana Sattu, 3 units" sends the packer to one bin for a pick
    that needs two. The product name alone is not the key — product PLUS pack size is.
    """
    orders = [_order(amazon_order_id="1",
                     items=[_item("B0CHANA500", 2), _item("B0CHANA1KG", 1)])]
    sheet = logic.picking_sheet(orders, CATALOGUE, TODAY)
    chana = [l for l in _lines(sheet) if l["product"] == "Chana Sattu"]
    assert len(chana) == 2, f"sizes collapsed into {len(chana)} line(s)"
    assert {l["weight_label"] for l in chana} == {"500g", "1 kg"}


def test_the_kilogram_total_multiplies_pack_size_by_quantity():
    """The number the courier and the vehicle care about, and it is NET.

    Deliberately asymmetric quantities: 24 x 500g and 12 x 1kg are BOTH 12 kg, so a test
    using equal weights could not tell a correct total from one that summed pack sizes
    without multiplying by quantity. Here the total is 24.0, where a
    sum-without-multiply would produce 1.5.
    """
    orders = [_order(amazon_order_id=str(i), items=[_item("B0CHANA500", 1)])
              for i in range(24)]
    orders += [_order(amazon_order_id=f"k{i}", items=[_item("B0RAGI1KG0", 1)])
               for i in range(12)]
    sheet = logic.picking_sheet(orders, CATALOGUE, TODAY)
    by_label = {l["weight_label"]: l for l in _lines(sheet)}
    assert by_label["500g"]["kg"] == pytest.approx(12.0)
    assert by_label["1 kg"]["kg"] == pytest.approx(12.0)
    assert _totals(sheet)["kg"] == pytest.approx(24.0)


def test_the_totals_row_equals_the_sum_of_the_lines():
    """A totals row that disagrees with its own lines is worse than none."""
    orders = [
        _order(amazon_order_id="1", items=[_item("B0CHANA500", 3)]),
        _order(amazon_order_id="2", items=[_item("B0POSTA100", 5)]),
    ]
    sheet = logic.picking_sheet(orders, CATALOGUE, TODAY)
    lines, totals = _lines(sheet), _totals(sheet)
    assert totals["quantity"] == sum(l["quantity"] for l in lines)
    assert totals["kg"] == pytest.approx(sum(l["kg"] for l in lines))


def test_an_unknown_asin_is_shown_and_named_not_dropped():
    """A missing picking-sheet row is stock nobody packs.

    An ASIN the catalogue does not know still gets a line — using Amazon's own title —
    flagged unknown, and reported in `unknown_asins` so the owner can fix the sheet.
    Silently dropping it means the parcel is never picked and nobody finds out until
    Amazon reports a late shipment.
    """
    orders = [_order(amazon_order_id="1", items=[_item("B0MYSTERY1", 4)])]
    sheet = logic.picking_sheet(orders, CATALOGUE, TODAY)
    line = next(l for l in _lines(sheet) if not l["known"])
    assert line["quantity"] == 4
    assert "B0MYSTERY1" in sheet["unknown_asins"]


def test_a_line_with_no_pack_size_is_excluded_from_kg_but_still_picked():
    """Counted in units, absent from the kilogram total, and named.

    Treating an unknown weight as 0 makes a 47 kg sheet quietly report 40 — a wrong
    number handed to a courier. The units still appear, because the parcel still has to
    be packed.
    """
    catalogue = dict(CATALOGUE)
    catalogue["B0NOWEIGHT"] = {"name": "Mystery Mix", "weight": 0, "brand": "Mithila Foods"}
    orders = [
        _order(amazon_order_id="1", items=[_item("B0CHANA500", 2)]),   # 1.0 kg
        _order(amazon_order_id="2", items=[_item("B0NOWEIGHT", 5)]),   # no weight
    ]
    sheet = logic.picking_sheet(orders, catalogue, TODAY)
    assert _totals(sheet)["kg"] == pytest.approx(1.0), "an unweighed line polluted the total"
    assert _totals(sheet)["quantity"] == 7, "the unweighed units must still be picked"
    assert _totals(sheet)["lines_without_weight"] == 1


def test_lines_are_ordered_by_quantity_descending():
    """The big picks lead, so the sheet reads top-down as a plan of work."""
    orders = [_order(amazon_order_id="1",
                     items=[_item("B0POSTA100", 1), _item("B0CHANA500", 9)])]
    sheet = logic.picking_sheet(orders, CATALOGUE, TODAY)
    assert [l["quantity"] for l in _lines(sheet)] == [9, 1]


def test_the_three_sections_split_by_the_physical_job():
    """to_pack / awaiting_pickup / later, each a different action.

    A shipped-and-delivered order appears in none of them — it needs nothing.
    """
    orders = [
        _order(amazon_order_id="a", items=[_item("B0CHANA500")]),                 # today
        _order(amazon_order_id="b", easyship_status="LabelGenerated",
               items=[_item("B0RAGI1KG0")]),                                      # pickup
        _order(amazon_order_id="c",
               latest_ship_date_utc=datetime(2026, 8, 27, 18, 29),
               items=[_item("B0POSTA100")]),                                      # later
        _order(amazon_order_id="d", status="Shipped", easyship_status="Delivered",
               items=[_item("B0CHANA1KG")]),                                      # done
    ]
    sheet = logic.picking_sheet(orders, CATALOGUE, TODAY)
    assert _totals(sheet, logic.BUCKET_TODAY)["orders"] == 1
    assert _totals(sheet, logic.BUCKET_PICKUP)["orders"] == 1
    assert _totals(sheet, logic.BUCKET_LATER)["orders"] == 1
    assert logic.BUCKET_DONE not in sheet["sections"], (
        "finished orders must not occupy a section on a picking sheet"
    )


def test_an_overdue_order_is_in_todays_section_and_flagged():
    """Overdue belongs in today's work, not a fourth box.

    A missed deadline should make today's sheet louder. Hiding it in its own section is
    how it gets scrolled past.
    """
    orders = [_order(amazon_order_id="late",
                     latest_ship_date_utc=datetime(2026, 8, 20, 18, 29),
                     items=[_item("B0CHANA500")])]
    sheet = logic.picking_sheet(orders, CATALOGUE, TODAY)
    assert _totals(sheet, logic.BUCKET_TODAY)["orders"] == 1
    assert _totals(sheet, logic.BUCKET_TODAY)["overdue_orders"] == 1
