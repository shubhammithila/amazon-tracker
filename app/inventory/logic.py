"""Units held at Amazon, from one FBA Inventory API summary. Pure.

**Every state counts and only damaged stock is left out**, the owner's rule (13 Oct 2026): stock is
at FBA once a shipment is created, inbound, being received, reserved, being researched, "or
anything". Measured that day: the Manage FBA Inventory CSV's nine summed columns read 7,711 units
against Amazon's own total less damaged of 10,636, and the whole 2,925-unit gap was inbound SHIPPED
stock on 17 SKUs (HF_gbrice_1kg_FBA: 0 in the CSV's columns, 340 inbound shipped here). This API
carries it; the CSV's `afn-inbound-shipped-quantity` column did not.
"""
from __future__ import annotations

from app.portfolio.logic import CHANNEL_FBA, _channel_of

#: The quantities stored per SKU, in the order they are stored. The last one is NOT held.
QUANTITY_FIELDS = ("fulfillable", "inbound_working", "inbound_shipped", "inbound_receiving",
                   "reserved", "researching", "future_supply", "unfulfillable")
HELD_FIELDS = QUANTITY_FIELDS[:-1]
#: The Shipment tab's own guard, by value: dozens of FBA SKUs all at zero is a failed read, a
#: handful is a genuinely empty warehouse. `tests/test_inventory.py` pins that the two agree.
ALL_ZERO_STOCK_THRESHOLD = 10


def _int(v) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _parts(d: dict) -> dict:
    fut = d.get("futureSupplyQuantity") or {}
    return {
        "fulfillable": _int(d.get("fulfillableQuantity")),
        "inbound_working": _int(d.get("inboundWorkingQuantity")),
        "inbound_shipped": _int(d.get("inboundShippedQuantity")),
        "inbound_receiving": _int(d.get("inboundReceivingQuantity")),
        # Reserved includes pending transshipment, i.e. the CSV's FC-to-FC transfer column.
        "reserved": _int((d.get("reservedQuantity") or {}).get("totalReservedQuantity")),
        "researching": _int((d.get("researchingQuantity") or {}).get("totalResearchingQuantity")),
        "future_supply": (_int(fut.get("reservedFutureSupplyQuantity"))
                          + _int(fut.get("futureSupplyBuyableQuantity"))),
        "unfulfillable": _int((d.get("unfulfillableQuantity") or {}).get("totalUnfulfillableQuantity")),
    }


def held_units(details: dict) -> int:
    parts = _parts(details or {})
    return sum(parts[k] for k in HELD_FIELDS)


def reduce_summaries(summaries: list[dict]) -> list[dict]:
    """FBA SKUs only, each with its quantities and `held`. A flex SKU's 999 fulfillable is a
    placeholder for stock on our own shelf, not stock at Amazon, so it never counts."""
    out = []
    for s in summaries:
        sku = str(s.get("sellerSku") or "").strip()
        asin = str(s.get("asin") or "").strip().upper()
        if not sku or not asin or _channel_of(sku) != CHANNEL_FBA:
            continue
        parts = _parts(s.get("inventoryDetails") or {})
        out.append({"seller_sku": sku, "asin": asin, "fnsku": s.get("fnSku") or "",
                    "amazon_updated_at": s.get("lastUpdatedTime") or "", **parts,
                    "held": sum(parts[k] for k in HELD_FIELDS)})
    return out


def units_by_asin(rows: list[dict]) -> dict[str, int]:
    """Summed across an ASIN's FBA SKUs: an ASIN can carry two, and dropping one under-states."""
    out: dict[str, int] = {}
    for r in rows:
        out[r["asin"]] = out.get(r["asin"], 0) + int(r["held"])
    return out


def looks_empty(rows: list[dict]) -> bool:
    return len(rows) >= ALL_ZERO_STOCK_THRESHOLD and not any(r["held"] for r in rows)
