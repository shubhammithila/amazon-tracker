"""FBA shipments report rows -> `customer_order_lines` rows. Pure: no I/O.

Six fields leave a row: the order and item ids, the purchase day, the SKU, the quantity and what
the customer paid for the line (`revenue`, for Customer value -> LTV).
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


def _revenue(row) -> float | None:
    """What the customer paid for the line, ex-GST, after the item promotion. Shipping excluded.

    Measured on 489 real rows: `item-price` is ALREADY ex-GST (₹177.14 with `item-tax` ₹8.86 beside
    it, a ₹186 shelf price) and agrees with Amazon's own ex-GST sales to 0.3% per unit. The
    promotion arrives NEGATIVE (−₹681.88 over two days), so it is added; taken as −|x| in case a
    report ever sends it positive. A blank price is unknown (None), never ₹0.
    """
    raw = (row.get("item-price") or "").strip()
    if not raw:
        return None
    try:
        price = float(raw)
        promo = float((row.get("item-promotion-discount") or "0").strip() or 0)
    except ValueError:
        return None
    return round(max(0.0, price - abs(promo)), 2)


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
            "revenue": _revenue(row),
        })
    return lines, counts
