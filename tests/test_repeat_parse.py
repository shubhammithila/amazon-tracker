"""Report rows -> lines. Column names are the REAL report's (probed 06 Oct 2026)."""
import pytest

from app.repeat import keys
from app.repeat.parse import parse_rows

pytestmark = pytest.mark.regression
MAP = {"1kg cs FBA": ("B0CHILD001", "B0PARENT01")}


def _row(**over):
    row = {"amazon-order-id": "171-1", "shipment-item-id": "D1",
           "purchase-date": "2026-09-30T20:00:00+00:00",
           "buyer-email": "abc@marketplace.amazon.in", "sku": "1kg cs FBA",
           "quantity-shipped": "2", "item-price": "180.00", "buyer-name": "",
           "ship-postal-code": "800001"}
    row.update(over)
    return row


def test_a_purchase_after_1830_utc_lands_on_the_NEXT_ist_day():
    lines, _ = parse_rows([_row()], "s", MAP)
    assert lines[0]["purchase_day"] == "2026-10-01"


def test_a_purchase_before_1830_utc_stays_on_the_same_ist_day():
    lines, _ = parse_rows([_row(**{"purchase-date": "2026-09-30T18:00:00+00:00"})], "s", MAP)
    assert lines[0]["purchase_day"] == "2026-09-30"


def test_the_line_carries_the_hashed_key_and_resolved_asins_and_no_pii():
    lines, _ = parse_rows([_row()], "s", MAP)
    line = lines[0]
    assert line["buyer_key"] == keys.customer_key("abc@marketplace.amazon.in", "s")
    assert (line["child_asin"], line["parent_asin"], line["units"]) == \
        ("B0CHILD001", "B0PARENT01", 2)
    assert "abc" not in repr(line) and "800001" not in repr(line)


@pytest.mark.parametrize("over, counter", [
    ({"purchase-date": ""}, "skipped_no_date"),
    ({"item-price": "0.00"}, "skipped_zero_price"),
    ({"buyer-email": ""}, "skipped_no_key"),
])
def test_unusable_rows_are_skipped_AND_counted(over, counter):
    lines, counts = parse_rows([_row(**over)], "s", MAP)
    assert lines == [] and counts[counter] == 1 and counts["rows_seen"] == 1


def test_an_unknown_sku_is_KEPT_unresolved_and_counted():
    lines, counts = parse_rows([_row(sku="new FBA")], "s", MAP)
    assert lines[0]["parent_asin"] is None and counts["unresolved_sku"] == 1


def test_a_blank_price_is_not_treated_as_a_replacement():
    lines, _ = parse_rows([_row(**{"item-price": ""})], "s", MAP)
    assert len(lines) == 1


def test_a_missing_shipment_item_id_falls_back_to_the_order_item_id():
    row = _row()
    row.pop("shipment-item-id")
    row["amazon-order-item-id"] = "OI9"
    assert parse_rows([row], "s", MAP)[0][0]["shipment_item_id"] == "OI9"
