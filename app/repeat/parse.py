"""FBA shipments report rows -> `customer_order_lines` rows. Pure: no I/O.

Only five fields leave a row: the order and item ids, the purchase day, the SKU and the quantity.
The buyer email becomes a salted key here and goes no further; names, phones and addresses in the
report are never read at all.
"""
from __future__ import annotations

from datetime import datetime, timezone

from app import ist
from app.repeat.keys import customer_key


def _ist_day(stamp: str) -> str:
    when = datetime.fromisoformat(stamp.strip().replace("Z", "+00:00"))
    if when.tzinfo:
        when = when.astimezone(timezone.utc).replace(tzinfo=None)
    return ist.day_of(when)


def _is_free(price: str | None) -> bool:
    """A free replacement ships at 0 and would otherwise read as a repeat purchase. Blank means
    unknown, which is NOT free. Measured: 167 units at price 0 in 90 days."""
    try:
        return (price or "").strip() != "" and float(price) == 0.0
    except ValueError:
        return False


def parse_rows(rows, salt, mapping):
    counts = {"rows_seen": 0, "skipped_no_date": 0, "skipped_zero_price": 0,
              "skipped_no_key": 0, "unresolved_sku": 0}
    lines = []
    for row in rows:
        counts["rows_seen"] += 1
        if not (row.get("purchase-date") or "").strip():
            counts["skipped_no_date"] += 1
            continue
        if _is_free(row.get("item-price")):
            counts["skipped_zero_price"] += 1
            continue
        key = customer_key(row.get("buyer-email"), salt)
        if not key:
            counts["skipped_no_key"] += 1
            continue
        sku = (row.get("sku") or "").strip()
        child, parent = mapping.get(sku, (None, None))
        if parent is None:
            counts["unresolved_sku"] += 1
        lines.append({
            "amazon_order_id": row["amazon-order-id"].strip(),
            "shipment_item_id": (row.get("shipment-item-id")
                                 or row.get("amazon-order-item-id") or "").strip(),
            "buyer_key": key,
            "purchase_day": _ist_day(row["purchase-date"]),
            "seller_sku": sku,
            "child_asin": child,
            "parent_asin": parent,
            "units": int(float(row.get("quantity-shipped") or 0)),
        })
    return lines, counts
