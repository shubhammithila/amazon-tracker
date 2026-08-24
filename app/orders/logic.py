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
BUCKET_PICKUP = "awaiting_pickup"   # labelled, not yet collected
BUCKET_LATER = "later"              # unshipped, due after today
BUCKET_DONE = "done"                # picked up, delivered, returned, cancelled

#: `PendingSchedule` means Amazon has no label for this order yet, so the physical job is
#: pick-pack-and-label. Measured: all 97 currently unshipped orders are PendingSchedule.
STATUS_PENDING_SCHEDULE = "PendingSchedule"

#: Easy Ship statuses that mean the order needs nothing from the warehouse today.
FINISHED_EASYSHIP = frozenset({
    "PickedUp", "Delivered", "ReturnedToSeller", "ReturningToSeller", "LabelCanceled",
})

#: Order statuses that still need packing. Anything else is off the floor.
OPEN_ORDER = frozenset({"Unshipped", "PartiallyShipped"})


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

    **`awaiting_pickup` is defined by exclusion, deliberately.** Across 90 days this
    account only ever showed `PendingSchedule`, `PickedUp`, `Delivered`,
    `ReturnedToSeller` and `LabelCanceled` — never `LabelGenerated` or `ReadyForPickup`,
    presumably because labels are generated and collected the same day. Hardcoding those
    two strings would produce a permanently empty section, so anything open that is NOT
    pending counts as labelled, and the raw status is rendered on the row so an
    unexpected value is visible rather than silently mis-filed.

    An order with no deadline is `later`, never `to_pack`: it is real work but not
    today's, and putting it in today's total would inflate the number the warehouse
    plans against.
    """
    easyship = (_field(order, "easyship_status") or "").strip()
    status = (_field(order, "status") or "").strip()

    if easyship in FINISHED_EASYSHIP or status not in OPEN_ORDER:
        return BUCKET_DONE

    if easyship and easyship != STATUS_PENDING_SCHEDULE:
        return BUCKET_PICKUP

    due = ship_by_date(order)
    if due is not None and due <= today:
        return BUCKET_TODAY
    return BUCKET_LATER
