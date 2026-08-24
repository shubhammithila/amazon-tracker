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
