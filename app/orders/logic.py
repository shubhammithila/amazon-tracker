"""Pure rules for the Orders tab. No database, no network.

Everything here was measured against the live account on 2026-08-24 rather than assumed,
and three of the four rules exist because the obvious version is wrong:

* `is_easy_ship` keys on `ShipServiceLevel`, not `FulfillmentChannel`.
* `ship_by_date` refuses Amazon's 1995 sentinel.
* `to_ist` exists because every deadline is 18:29Z = 23:59 IST.

The fourth, `picking_sheet`, is the feature: item + pack size + brand, with quantity,
order counts and a net kilogram total.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta, timezone

from app.shipment.logic import weight_label

#: India Standard Time. A fixed offset — India has no DST, so a full tzdata lookup would
#: add a dependency for no behaviour.
IST = timezone(timedelta(hours=5, minutes=30))

#: Amazon sends 1995-01-01T00:00:00Z as "no Easy Ship ship-by". Three real `S02-…`
#: orders carry it. Rendered as a date it reads as 31 years overdue and sorts to the top
#: of the packer's sheet, so it is treated as absent.
SENTINEL_YEAR = 1995

#: Easy Ship service levels all contain this token: "Std IN EZ National COD",
#: "Std IN EZ Remote", "Std IN EZ Metro COD". Plain self-ship reads "Standard".
EASY_SHIP_TOKEN = "EZ"

BUCKET_TODAY = "to_pack"            # unshipped, no label yet, due today or overdue
BUCKET_PICKUP = "awaiting_pickup"   # labelled, courier has not collected it
BUCKET_LATER = "later"              # unshipped, due after today
BUCKET_PENDING = "pending_payment"  # Amazon has not confirmed the payment yet
BUCKET_DONE = "done"                # picked up, delivered, returned, cancelled

#: `PendingSchedule` means Amazon has no label for this order yet, so the physical job is
#: pick-pack-and-label. Every `Unshipped` Easy Ship order observed carries it.
STATUS_PENDING_SCHEDULE = "PendingSchedule"

#: Labelled, boxed, and waiting for Amazon Logistics to collect. **The real value on
#: amazon.in is `PendingPickUp`.**
#:
#: This cost a live bug. The first version keyed on `LabelGenerated` and `ReadyForPickup` —
#: names taken from Amazon's DOCUMENTATION — and the section was permanently empty while
#: Seller Central showed 247 orders waiting for pickup. The doc names were never sent.
#: The mirror of the trap already recorded for the shipment API, where a value Amazon
#: RETURNS was not one it ACCEPTS: here, a value Amazon DOCUMENTS is not one it SENDS.
#:
#: The doc names are kept as aliases rather than deleted: they cost nothing, and Amazon may
#: legitimately use them in another marketplace.
AWAITING_PICKUP_EASYSHIP = frozenset({
    "PendingPickUp",                        # measured on amazon.in
    "LabelGenerated", "ReadyForPickup",      # documented; never observed here
})

#: Easy Ship statuses that mean the order needs nothing from the warehouse.
#: `ReturningToSeller` is finished for PICKING purposes — the parcel is on its way back and
#: nothing is packed for it — even though the order is not over commercially.
FINISHED_EASYSHIP = frozenset({
    "PickedUp", "Delivered", "ReturnedToSeller", "ReturningToSeller", "LabelCanceled",
})

#: Order statuses that still need packing.
OPEN_ORDER = frozenset({"Unshipped", "PartiallyShipped"})

#: Payment not yet confirmed by Amazon, so the order cannot be packed and its items are not
#: final. Shown in its own section rather than hidden: it is what is coming next.
PENDING_ORDER = frozenset({"Pending"})

#: `Shipped` covers BOTH "labelled and awaiting pickup" AND "long since delivered", which is
#: why the Easy Ship status decides the bucket rather than the order status. Getting this
#: backwards is what made the 247 waiting-for-pickup orders invisible.
SHIPPED_ORDER = frozenset({"Shipped"})


def _field(row, name, default=None):
    """Read a field from either a dict or an ORM row.

    The same dual shape the shipment aggregations handle: the repository hands out dicts
    so these functions can be tested without a database, but an ORM object must work too.
    """
    return row.get(name, default) if isinstance(row, Mapping) else getattr(row, name, default)


def to_ist(value: datetime | None) -> datetime | None:
    """A UTC timestamp as IST. `None` passes through.

    **A naive value is treated as UTC**, because that is what it is: SQLAlchemy's
    DateTime drops the tzinfo, so a row read back from the database has none even though
    it was stored as UTC. Assuming local instead would subtract 5.5 hours from every
    deadline — a uniform error, and therefore the hardest kind to notice.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(IST)


def is_easy_ship(ship_service_level) -> bool:
    """Is this an Easy Ship order?

    Keyed on the service level because `FulfillmentChannel` reads MFN for BOTH Easy Ship
    and plain self-ship. Filtering on the channel admits three real `S02-…` "Standard"
    orders whose ship-by is the 1995 sentinel.

    Split on whitespace rather than a substring test, so a product name that happens to
    contain "ez" cannot match.
    """
    return EASY_SHIP_TOKEN in (ship_service_level or "").upper().split()


def ship_by_date(order) -> date | None:
    """The ship-by deadline as an IST calendar date, or None when there is none.

    None covers both a missing timestamp and Amazon's 1995 sentinel. Callers must treat
    None as "no deadline" — never as "overdue", which is what a naive date comparison
    would conclude.
    """
    raw = _field(order, "latest_ship_date_utc")
    local = to_ist(raw)
    if local is None or local.year <= SENTINEL_YEAR:
        return None
    return local.date()


def bucket_for(order, today: date) -> str:
    """Which section of the picking sheet this order belongs in.

    The two actionable buckets are two different PHYSICAL jobs, which is why they are
    separate sections rather than one "not done" list:

    * ``to_pack`` — pick it, pack it, generate the label in Seller Central.
    * ``awaiting_pickup`` — already boxed and labelled; hand it to the courier.

    * ``pending_payment`` — Amazon has not confirmed payment; nothing can be packed yet.

    **The EASY SHIP status decides the bucket, not the order status.** `Shipped` covers both
    "labelled and sitting on the floor waiting for the courier" (`PendingPickUp`) and "was
    delivered a fortnight ago" (`Delivered`). Branching on the order status put all of them
    in `done`, which is how 247 orders waiting for pickup became invisible to a screen whose
    job is to show exactly that.

    An order with no deadline is `later`, never `to_pack`: it is real work but not today's,
    and putting it in today's total would inflate the number the warehouse plans against.
    """
    easyship = (_field(order, "easyship_status") or "").strip()
    status = (_field(order, "status") or "").strip()

    # Checked FIRST, because these are the boxes physically standing in the warehouse
    # waiting to be handed over — and they arrive with status `Shipped`, which any
    # order-status-first test would file as finished.
    if easyship in AWAITING_PICKUP_EASYSHIP:
        return BUCKET_PICKUP

    if easyship in FINISHED_EASYSHIP:
        return BUCKET_DONE

    if status in PENDING_ORDER:
        return BUCKET_PENDING

    if status not in OPEN_ORDER:
        # `Shipped` with an Easy Ship status we do not recognise, or `Canceled`. Treated as
        # finished rather than guessed into a picking section: a wrong row on the sheet
        # sends someone to a shelf for a parcel that has already gone.
        return BUCKET_DONE

    due = ship_by_date(order)
    if due is not None and due <= today:
        return BUCKET_TODAY
    return BUCKET_LATER


#: The sections a picking sheet renders, in the order the warehouse works them. `done` is
#: absent on purpose: a finished order needs nothing, and a section for it is a section
#: the packer scrolls past every morning.
SHEET_SECTIONS = (BUCKET_TODAY, BUCKET_PICKUP, BUCKET_LATER, BUCKET_PENDING)


def _catalogue_entry(catalogue, asin: str) -> dict | None:
    return (catalogue or {}).get((asin or "").strip().upper())


def picking_sheet(orders: Sequence, catalogue: Mapping, today: date) -> dict:
    """Today's work, aggregated by product + pack size + brand.

    Returns::

        {"sections": {bucket: {"lines": [...], "totals": {...}}},
         "unknown_asins": [asin, ...]}

    Each line carries ``product, weight, weight_label, brand, quantity, orders, kg,
    known``; each totals block carries ``quantity, orders, kg, lines_without_weight,
    overdue_orders``.

    Four properties are load-bearing:

    **Product PLUS pack size is the key.** 500 g and 1 kg of one product are different
    shelves, so they are different lines. Keying on the name alone would send the packer
    to one bin for a pick that needs two.

    **`orders` counts ORDERS, not lines.** Two units of one SKU in one order is quantity
    2, orders 1. That distinction is what makes "24 units across 22 orders" mean
    something: it is the parcel count as well as the pick count.

    **`kg` is pack size x quantity, and NET.** Cartons, filler and tape are not in the
    catalogue, so a weighbridge reads higher — the same caveat ``logic.shipment_weight``
    carries on the shipment side. A line whose pack size is unknown is EXCLUDED from the
    kilogram total and counted in ``lines_without_weight``: treating it as 0 makes a
    47 kg sheet quietly report 40, and a wrong weight reaches a courier.

    **An unknown ASIN is kept, flagged, and named.** A row missing from a picking sheet is
    stock nobody packs, discovered when Amazon reports a late shipment.
    """
    buckets: dict[str, dict] = {
        name: {"lines": {}, "orders": set(), "overdue": set()} for name in SHEET_SECTIONS
    }
    unknown: set[str] = set()

    for order in orders:
        bucket = bucket_for(order, today)
        if bucket not in buckets:
            continue
        holder = buckets[bucket]
        order_id = _field(order, "amazon_order_id") or ""
        holder["orders"].add(order_id)

        due = ship_by_date(order)
        if bucket == BUCKET_TODAY and due is not None and due < today:
            holder["overdue"].add(order_id)

        for item in _field(order, "items") or []:
            asin = (_field(item, "asin") or "").strip().upper()
            if not asin:
                continue
            quantity = int(_field(item, "quantity_ordered") or 0)
            if quantity <= 0:
                continue

            entry = _catalogue_entry(catalogue, asin)
            if entry is None:
                unknown.add(asin)
                product = (_field(item, "title") or asin)[:60]
                weight, brand, known = 0.0, "", False
            else:
                product = entry.get("name") or asin
                weight = float(entry.get("weight") or 0)
                raw_brand = str(entry.get("brand") or "")
                # "MF"/"HF" as the rest of the app writes them, from the sheet's full
                # names. Substring, not equality: the sheet says "Mithila Foods".
                brand = "MF" if "mithila" in raw_brand.lower() else ("HF" if raw_brand else "")
                known = True

            key = (product, weight, brand, known)
            line = holder["lines"].setdefault(
                key, {"product": product, "weight": weight, "brand": brand,
                      "known": known, "quantity": 0, "orders": set()}
            )
            line["quantity"] += quantity
            line["orders"].add(order_id)

    sections: dict[str, dict] = {}
    for name in SHEET_SECTIONS:
        holder = buckets[name]
        lines = []
        for line in holder["lines"].values():
            weight = float(line["weight"] or 0)
            lines.append({
                "product": line["product"],
                "weight": weight,
                "weight_label": weight_label(weight) if weight else "",
                "brand": line["brand"],
                "quantity": line["quantity"],
                "orders": len(line["orders"]),
                "kg": round(weight * line["quantity"], 3) if weight else None,
                "known": line["known"],
            })
        # Quantity descending, then product name, so the big picks lead and the order is
        # deterministic between two renders of the same data.
        lines.sort(key=lambda row: (-row["quantity"], row["product"].casefold()))
        sections[name] = {
            "lines": lines,
            "totals": {
                "quantity": sum(row["quantity"] for row in lines),
                "orders": len(holder["orders"]),
                "kg": round(sum(row["kg"] or 0 for row in lines), 3),
                "lines_without_weight": sum(1 for row in lines if row["kg"] is None),
                "overdue_orders": len(holder["overdue"]),
            },
        }

    return {"sections": sections, "unknown_asins": sorted(unknown)}
