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

#: Easy Ship statuses that mean Amazon HAS a label for the order — so it has been shipped on
#: the portal and is the warehouse's parcel rather than the ecom team's to-do.
#:
#: **`PendingSchedule` is deliberately absent.** No label yet means the order is still
#: sitting in Unshipped on Seller Central, which is the ecom team's job; putting it on the
#: warehouse's dispatch sheet would ask the floor to box something that has no label to stick
#: on it.
#:
#: `LabelCanceled` is absent too: the label existed and was withdrawn, so there is no parcel.
LABELLED_EASYSHIP = frozenset({
    "PendingPickUp",                            # labelled, courier has not come yet
    "PickedUp", "OutForDelivery", "Delivered",   # collected — still today's dispatch
    "ReturningToSeller", "ReturnedToSeller",     # went out, coming back
    "LabelGenerated", "ReadyForPickup",          # documented aliases; never seen here
})


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


# ─── Today's dispatch: the warehouse's own question ──────────────────────────
#
# `bucket_for` answers "what state is this order in", and every order gets exactly ONE
# bucket. The warehouse asks something different — "is this parcel part of today's
# dispatch?" — and that question CROSSES two buckets: a labelled order not yet collected is
# `awaiting_pickup`, and the same order an hour after the courier came is `done`.
#
# So this is a separate predicate rather than a change to `bucket_for`. Editing the bucketing
# would silently move the picking sheet's four section totals and its Excel export, and it is
# pinned by the tests that caught the 247-order bug. An orthogonal question costs nothing.


def is_todays_dispatch(order, today: date) -> bool:
    """Is this order part of today's physical dispatch?

    Two conditions, and both matter:

    * **The ship-by date is today.** That is the pickup slot — every real `LatestShipDate`
      on this account is `18:29Z` = `23:59 IST`, so "due today" is a calendar day in India.
    * **Amazon has a label** (`LABELLED_EASYSHIP`). An order still in `Unshipped` /
      `PendingSchedule` belongs to the ecom team, who ship it on Seller Central; the
      warehouse cannot box a parcel that has no label yet.

    **Collected orders stay.** This is the whole point, and it is why the rule is not simply
    "awaiting pickup": measured on production, 200 of 264 orders flipped from `PendingPickUp`
    to `PickedUp` overnight. A list keyed on "not yet collected" therefore empties itself
    through the day and takes the day's packed tally with it — so the floor would lose the
    record of what it packed at the moment the courier arrived. What was packed this morning
    is still what was packed this morning.
    """
    if (_field(order, "easyship_status") or "").strip() not in LABELLED_EASYSHIP:
        return False
    return ship_by_date(order) == today


def _brand_code(raw_brand) -> str:
    """"MF"/"HF" as the rest of the app writes them, from the sheet's full names.

    Substring rather than equality, because the sheet says "Mithila Foods".
    """
    text = str(raw_brand or "")
    return "MF" if "mithila" in text.lower() else ("HF" if text else "")


def dispatch_sheet(
    orders: Sequence, catalogue: Mapping, today: date, packed: Mapping | None = None
) -> dict:
    """Today's dispatch, grouped parent product → pack size, heaviest first.

    Asked for as: *"Each parent item total weight orders. like ABC sattu ka aaj ka kitna
    total weight ka order hai … uske niche 500g, 1kg - kitne kitne units. sort it total
    weight wise."*

    Returns::

        {"parents": [{"product", "brand", "kg", "units", "orders", "packed",
                      "sizes": [{...}]}],
         "orders":  [{"amazon_order_id", "parent", "product", "seller_sku", ...}],
         "totals":  {...},
         "unknown_asins": [...]}

    Four properties are load-bearing:

    **The PARENT is the catalogue name; the SIZE is the pack.** "ABC Sattu" is one heading
    with 500 g and 1 kg beneath it, because that is how the owner thinks about the day's
    volume — while the packer still needs the sizes apart, since they live on different
    shelves.

    **Sorted by KILOGRAMS descending**, parents and sizes both. Weight is what fills the
    vehicle, so the heaviest line is the one worth reading first. Quantity would put 21
    sachets above 15 kg of rice.

    **`kg` is pack size x quantity, and NET.** Cartons, filler and tape are not in the
    catalogue, so a weighbridge reads higher — the same caveat ``picking_sheet`` and
    ``shipment_weight`` carry. A size with no known pack weight is EXCLUDED from the kilogram
    total and counted in ``sizes_without_weight``: treating it as 0 makes a 47 kg sheet
    quietly report 40, and a wrong weight reaches a courier.

    **`packed` comes from OUR rows, not Amazon's.** It is the warehouse's own progress note
    against each ASIN and it never contradicts Amazon: no status is written, no invoice is
    raised from it. Amazon genuinely does not know how many units are in a box on this floor.
    """
    packed_by_asin = {
        str(asin or "").strip().upper(): int(units or 0)
        for asin, units in (packed or {}).items()
    }

    # (parent, weight) -> size accumulator. Keyed on the pair because two sizes of one
    # product must never collapse, and two products of one weight must never merge.
    sizes: dict[tuple, dict] = {}
    order_rows: list[dict] = []
    unknown: set[str] = set()
    order_ids: set[str] = set()

    for order in orders:
        if not is_todays_dispatch(order, today):
            continue
        order_id = _field(order, "amazon_order_id") or ""
        order_ids.add(order_id)

        for item in _field(order, "items") or []:
            asin = (_field(item, "asin") or "").strip().upper()
            if not asin:
                continue
            quantity = int(_field(item, "quantity_ordered") or 0)
            if quantity <= 0:
                continue

            entry = _catalogue_entry(catalogue, asin)
            if entry is None:
                # Kept and NAMED rather than dropped: a row missing from a dispatch sheet is
                # a parcel nobody packs, discovered when Amazon reports a late shipment.
                unknown.add(asin)
                parent = (_field(item, "title") or asin)[:60]
                weight, brand, known = 0.0, "", False
            else:
                parent = entry.get("name") or asin
                weight = float(entry.get("weight") or 0)
                brand = _brand_code(entry.get("brand"))
                known = True

            seller_sku = _field(item, "seller_sku") or ""
            key = (parent, weight, known)
            size = sizes.setdefault(key, {
                "parent": parent, "weight": weight, "brand": brand, "known": known,
                "asins": {}, "units": 0, "orders": set(), "seller_skus": set(),
            })
            size["units"] += quantity
            size["orders"].add(order_id)
            size["asins"][asin] = size["asins"].get(asin, 0) + quantity
            if seller_sku:
                size["seller_skus"].add(seller_sku)

            order_rows.append({
                "amazon_order_id": order_id,
                "parent": parent,
                "weight": weight,
                "weight_label": weight_label(weight) if weight else "",
                "seller_sku": seller_sku,
                "asin": asin,
                "quantity": quantity,
                "known": known,
                "city": _field(order, "city") or "",
                "state": _field(order, "state") or "",
                "easyship_status": _field(order, "easyship_status") or "",
            })

    # ── Roll the sizes up into parents ──
    parents: dict[str, dict] = {}
    for size in sizes.values():
        weight = float(size["weight"] or 0)
        # One ASIN per (parent, size) in practice; if the catalogue ever maps two, the
        # heavier-selling one names the row and both are still counted in `units`.
        asin = max(size["asins"].items(), key=lambda kv: kv[1])[0] if size["asins"] else ""
        packed_units = sum(packed_by_asin.get(a, 0) for a in size["asins"])
        row = {
            "asin": asin,
            "weight": weight,
            "weight_label": weight_label(weight) if weight else "",
            "seller_sku": sorted(size["seller_skus"])[0] if size["seller_skus"] else "",
            "units": size["units"],
            "orders": len(size["orders"]),
            "kg": round(weight * size["units"], 3) if weight else None,
            "packed": packed_units,
            # Clamped at 0 the way `remaining_for` is: this number reaches a printed sheet,
            # and "-5 to pack" is not a quantity. Over-packing is reported separately.
            "remaining": max(0, size["units"] - packed_units),
            "over_packed": max(0, packed_units - size["units"]),
            "known": size["known"],
        }
        parent = parents.setdefault(size["parent"], {
            "product": size["parent"], "brand": size["brand"], "known": size["known"],
            "sizes": [], "units": 0, "kg": 0.0, "packed": 0, "orders": set(),
        })
        parent["sizes"].append(row)
        parent["units"] += row["units"]
        parent["kg"] += row["kg"] or 0
        parent["packed"] += packed_units
        parent["orders"] |= size["orders"]

    parent_rows = []
    for parent in parents.values():
        parent["sizes"].sort(key=_size_sort_key)
        parent_rows.append({
            "product": parent["product"],
            "brand": parent["brand"],
            "known": parent["known"],
            "units": parent["units"],
            "kg": round(parent["kg"], 3),
            "packed": parent["packed"],
            "remaining": max(0, parent["units"] - parent["packed"]),
            "orders": len(parent["orders"]),
            "sizes": parent["sizes"],
        })
    # Heaviest parent first — weight is what fills the vehicle. Name breaks the tie so two
    # renders of the same data agree.
    parent_rows.sort(key=lambda row: (-row["kg"], row["product"].casefold()))

    # ── Mark the lines that share an order ──
    #
    # An order holding two products rendered as two unconnected rows with the same id, and
    # nothing on either row said a second item existed — so the parcel could be packed, ticked
    # and handed over half full. Measured on 2026-08-27: 85 orders, 86 lines, so exactly one
    # parcel that day had this shape. Rare is not harmless: it is precisely the order that ships
    # short, and the one nobody is looking for.
    #
    # Computed HERE rather than in the template because the PDF and the Excel need the same
    # fact, and three renderers deriving it separately is how they start to disagree.
    lines_per_order: dict[str, int] = {}
    for row in order_rows:
        oid = row["amazon_order_id"]
        lines_per_order[oid] = lines_per_order.get(oid, 0) + 1
    for row in order_rows:
        row["order_lines"] = lines_per_order.get(row["amazon_order_id"], 1)
        row["multi_item"] = row["order_lines"] > 1

    # The order list follows the SAME parent order as the summary, so the two halves of the
    # printed sheet read together instead of being two independent sorts.
    parent_order = {row["product"]: index for index, row in enumerate(parent_rows)}

    def line_rank(row: dict) -> tuple:
        return (parent_order.get(row["parent"], len(parent_order)), -float(row["weight"] or 0))

    # **An order's lines must stay ADJACENT, or grouping them on screen is impossible.** Sorting
    # by parent alone scatters them: a two-product order has two different parents, so its lines
    # land wherever each product sorts — 40 rows apart in the measured case. Each order is
    # therefore ANCHORED at its best-ranking line and its lines travel together.
    #
    # Anchoring on the heaviest line rather than the lightest keeps the heaviest-first reading
    # order the rest of the sheet uses, so a multi-item order does not sink below single-item
    # ones it outweighs.
    anchor: dict[str, tuple] = {}
    for row in order_rows:
        oid = row["amazon_order_id"]
        rank = line_rank(row)
        if oid not in anchor or rank < anchor[oid]:
            anchor[oid] = rank
    order_rows.sort(key=lambda row: (
        anchor[row["amazon_order_id"]],
        row["amazon_order_id"],   # ties broken stably, so two renders agree
        line_rank(row),           # within one order: heaviest item first
    ))

    all_sizes = [size for row in parent_rows for size in row["sizes"]]
    return {
        "parents": parent_rows,
        "orders": order_rows,
        "totals": {
            "orders": len(order_ids),
            # The row count, stated separately, because it is NOT the order count and the two
            # were silently disagreeing on screen: "Orders today 86" beside a tab reading
            # "Orders (87)". Both were right; neither was labelled.
            "order_lines": len(order_rows),
            "multi_item_orders": sum(1 for count in lines_per_order.values() if count > 1),
            "units": sum(size["units"] for size in all_sizes),
            "kg": round(sum(size["kg"] or 0 for size in all_sizes), 3),
            "packed": sum(size["packed"] for size in all_sizes),
            "remaining": sum(size["remaining"] for size in all_sizes),
            "over_packed": sum(size["over_packed"] for size in all_sizes),
            "sizes_without_weight": sum(1 for size in all_sizes if size["kg"] is None),
            "parents": len(parent_rows),
        },
        "unknown_asins": sorted(unknown),
    }


def _size_sort_key(size: dict) -> tuple:
    """Heaviest size first within a parent; label breaks the tie.

    A size with no known pack weight sorts last rather than first: `None` would compare as
    the lightest under `-kg` and put the one row nobody can weigh at the top of the sheet.
    """
    return (-(size["kg"] or 0), -(float(size["weight"] or 0)), size["weight_label"])


# ─── Purchasing: today's ordered weight against raw material on hand ─────────


def to_buy_kg(ordered_kg, raw_kg) -> float:
    """The shortfall for one product, in kilograms. Never negative.

    Clamped at 0 for the same reason `remaining_for` clamps: this number reaches a purchasing
    list, and "-7 kg" is not a quantity anyone can order. A surplus is simply nothing to buy.
    """
    shortfall = float(ordered_kg or 0) - float(raw_kg or 0)
    return round(shortfall, 2) if shortfall > 0 else 0.0


def raw_stock_summary(sheet: Mapping, raw_stock: Mapping) -> dict:
    """Today's ordered weight per parent against raw stock on hand, and what to buy.

    Takes the sheet `dispatch_sheet` already produced rather than re-reading orders, so the
    purchasing tab cannot disagree with the SKU tab about how much of a product is due. Row
    order is inherited untouched — parents are already heaviest first, and re-sorting here would
    make the two tabs lead with different products.

    Returns::

        {"rows": [{"product", "brand", "ordered_kg", "raw_kg", "to_buy_kg", "covered"}],
         "totals": {"ordered_kg", "raw_kg", "to_buy_kg", "short_products"}}

    **The to-buy TOTAL sums the clamped rows; it is NOT `total_ordered - total_raw`.** Those two
    differ the moment any product is in surplus, and the subtraction is wrong because it lets a
    surplus of ABC Sattu cancel a shortfall of Usna Chawal — you cannot make rice out of sattu.
    On real numbers the difference was 43.00 kg against 33.50 kg, and only the first is a
    purchasing quantity. Caught reviewing the design rather than the code: the wrong version
    looks entirely plausible in a totals row.

    A product with no entry in `raw_stock` counts as 0 on hand, not "unknown". Absent has to
    mean "buy all of it", because skipping it would drop the product off the purchasing list and
    a stockout is what costs the Buy Box.
    """
    lookup = {
        str(product or "").strip(): float(value or 0)
        for product, value in (raw_stock or {}).items()
    }

    rows = []
    for parent in sheet.get("parents") or []:
        product = parent["product"]
        ordered = round(float(parent.get("kg") or 0), 2)
        raw = round(lookup.get(product, 0.0), 2)
        shortfall = to_buy_kg(ordered, raw)
        rows.append({
            "product": product,
            "brand": parent.get("brand") or "",
            "ordered_kg": ordered,
            "raw_kg": raw,
            "to_buy_kg": shortfall,
            "covered": shortfall == 0.0,
        })

    return {
        "rows": rows,
        "totals": {
            "ordered_kg": round(sum(row["ordered_kg"] for row in rows), 2),
            "raw_kg": round(sum(row["raw_kg"] for row in rows), 2),
            # Sum of the CLAMPED rows. See the docstring.
            "to_buy_kg": round(sum(row["to_buy_kg"] for row in rows), 2),
            "short_products": sum(1 for row in rows if not row["covered"]),
        },
    }
