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
