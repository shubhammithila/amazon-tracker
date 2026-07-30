"""Pure shipment-planning rules. No FastAPI, no database, no I/O.

Every number the shipment feature decides is computed here, so the dashboard,
the four downloads and the invoice bridge cannot disagree with each other. If
you find yourself rounding or sorting anywhere else, move it in here instead.

Vocabulary, because two of these are easy to conflate:

  planned    what the plan says to send to Amazon for a SKU
  packed     units physically boxed, INCLUDING days that are on hold
  shippable  units on days cleared to ship, EXCLUDING held days
  remaining  planned - packed  (what is still to be boxed)

`packed` and `shippable` must stay separate. A held day's units exist in the
warehouse — telling the floor to pack them again would double-pack the order —
but they must not appear in a shipment until the day is released.
"""
from decimal import Decimal, ROUND_HALF_UP
from typing import Iterable, Mapping, Sequence

# Day statuses. A day is created `open`, ops `submitted` it, the owner
# `verified` it, and it becomes `shipped` once an invoice is attached. `held`
# means it was too small to ship on its own and is waiting to be combined with
# a later day's packing.
STATUS_OPEN = "open"
STATUS_SUBMITTED = "submitted"
STATUS_HELD = "held"
STATUS_VERIFIED = "verified"
STATUS_SHIPPED = "shipped"

#: Statuses whose units may go into a shipment. `open` is excluded because ops
#: has not finished the day; `held` because it is deliberately parked.
SHIPPABLE_STATUSES = frozenset({STATUS_SUBMITTED, STATUS_VERIFIED, STATUS_SHIPPED})

#: Only verified days can be invoiced — that approval is what gates the
#: legally-sequential GST number.
INVOICEABLE_STATUSES = frozenset({STATUS_VERIFIED})

DEFAULT_MIN_CARTONS = 25
DEFAULT_MIN_UNITS = 500

ROUNDING_STEP = 10


def round_to_step(value, step: int = ROUNDING_STEP) -> int:
    """Round to the nearest `step`, halves up, with a floor of one step.

    Two deliberate departures from plain arithmetic:

    1. Decimal ROUND_HALF_UP instead of the built-in round(), which does
       banker's rounding and rounds halves toward the even multiple:

           round(25 / 10) * 10 == 20    # wrong, reads as a bug on screen
           round_to_step(25)   == 30

    2. A non-zero need never rounds down to nothing. Strict nearest-10 would
       turn a need of 4 units into 0, dropping that SKU from the shipment while
       Amazon is actually short of it — a stockout on a slow SKU costs the Buy
       Box, which is far more expensive than shipping 6 extra units. So:

           round_to_step(0) == 0     # nothing needed really is nothing
           round_to_step(1) == 10    # floored to one step
           round_to_step(4) == 10
           round_to_step(14) == 10   # above the floor, normal nearest-10

    A quantity of exactly 0 stays 0: "nothing to send" is not "send 10".
    """
    if step <= 0:
        raise ValueError(f"step must be positive, got {step}")
    try:
        amount = Decimal(str(value))
    except Exception:
        return 0
    if amount <= 0:
        return 0
    steps = (amount / Decimal(step)).quantize(Decimal(1), rounding=ROUND_HALF_UP)
    # max(1, ...) is the floor: a real need must survive rounding.
    return max(1, int(steps)) * step


def round_to_10(value) -> int:
    """Round a plan quantity to the nearest 10, halves up.

    Kept as a named wrapper because the business rule ("nearest 10") is likely
    to become "nearest carton multiple" later. Changing it here changes it for
    the dashboard and all four documents at once.
    """
    return round_to_step(value, ROUNDING_STEP)


def _weight_of(item) -> float:
    """Weight as a float, tolerating Decimal columns, strings and None."""
    raw = item.get("weight") if isinstance(item, Mapping) else getattr(item, "weight", None)
    if raw in (None, ""):
        return 0.0
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


def _text_of(item, *names: str) -> str:
    for name in names:
        raw = item.get(name) if isinstance(item, Mapping) else getattr(item, name, None)
        if raw:
            return str(raw)
    return ""


def sort_key(item) -> tuple:
    """Order rows product-wise, then weight-wise, then by ASIN.

    Casefolded so the ordering does not depend on how a product name happens to
    be capitalised in product_families.json ('sattu' vs 'Jau Sattu'). ASIN is
    the final tiebreak purely so the order is deterministic — two SKUs with the
    same product and weight would otherwise sort arbitrarily and the row order
    could differ between the screen and a download.

    Accepts either a dict or an ORM object, so callers can use it before or
    after persistence.
    """
    product = _text_of(item, "sort_product", "item", "parent_product")
    return (product.casefold(), _weight_of(item), _text_of(item, "asin"))


def sort_items(items: Iterable) -> list:
    """Product-then-weight ordering. The only ordering used anywhere."""
    return sorted(items, key=sort_key)


def packed_units(entries: Iterable) -> int:
    """Total units boxed across the given packing entries (held days included)."""
    total = 0
    for entry in entries:
        raw = entry.get("units") if isinstance(entry, Mapping) else getattr(entry, "units", 0)
        total += int(raw or 0)
    return total


def packed_cartons(entries: Iterable) -> int:
    """Total cartons boxed across the given packing entries."""
    total = 0
    for entry in entries:
        raw = entry.get("cartons") if isinstance(entry, Mapping) else getattr(entry, "cartons", 0)
        total += int(raw or 0)
    return total


def remaining_for(planned, packed: int) -> int:
    """How much is still to be boxed. Never negative.

    Over-packing is clamped to 0 rather than shown as a negative: the warehouse
    sheet should read "nothing left to do", not "-40 to pack".
    """
    try:
        target = int(planned or 0)
    except (TypeError, ValueError):
        target = 0
    return max(0, target - max(0, int(packed or 0)))


def is_held(
    total_cartons: int,
    total_units: int,
    min_cartons: int = DEFAULT_MIN_CARTONS,
    min_units: int = DEFAULT_MIN_UNITS,
) -> bool:
    """Is this day's packing too small to ship on its own?

    AND, not OR — a day is only not worth shipping when it is short on BOTH
    counts. These products range from 500g pouches to 5kg bags, so either axis
    alone gives false holds:

        30 cartons /  300 units  ->  ships (heavy bags, carton count is fine)
        15 cartons /  900 units  ->  ships (small pouches, unit count is fine)
        20 cartons /  400 units  ->  held  (short both ways)

    A completely empty day is not "held", it is simply empty; callers keep it
    `open` rather than parking it.
    """
    cartons = max(0, int(total_cartons or 0))
    units = max(0, int(total_units or 0))
    if cartons == 0 and units == 0:
        return False
    return cartons < int(min_cartons or 0) and units < int(min_units or 0)


def hold_reason(
    total_cartons: int,
    total_units: int,
    min_cartons: int = DEFAULT_MIN_CARTONS,
    min_units: int = DEFAULT_MIN_UNITS,
) -> str:
    """Human-readable explanation shown next to a held day."""
    return (
        f"Only {total_cartons} cartons / {total_units} units "
        f"(below {min_cartons} cartons and {min_units} units) "
        "— combine with the next day's packing."
    )


def units_by_asin(days: Sequence, statuses=None) -> dict[str, int]:
    """Units per ASIN, optionally restricted to days in `statuses`.

    The three named wrappers below differ ONLY in which statuses they count, so
    they share this one loop. Three copies of it is how "packed" and "shippable"
    quietly start disagreeing about, say, whether a blank ASIN contributes.

    `statuses=None` means every day regardless of status.
    """
    totals: dict[str, int] = {}
    for day in days:
        if statuses is not None:
            status = (
                day.get("status") if isinstance(day, Mapping) else getattr(day, "status", None)
            )
            if status not in statuses:
                continue
        entries = day.get("entries") if isinstance(day, Mapping) else getattr(day, "entries", [])
        for entry in entries or []:
            asin = (
                entry.get("asin") if isinstance(entry, Mapping) else getattr(entry, "asin", "")
            ) or ""
            units = entry.get("units") if isinstance(entry, Mapping) else getattr(entry, "units", 0)
            if not asin:
                continue
            totals[asin] = totals.get(asin, 0) + int(units or 0)
    return totals


def shippable_units_by_asin(days: Sequence) -> dict[str, int]:
    """Units per ASIN across days cleared to ship. Held days are excluded.

    This is also how requirement 9's combining works: a held day simply is not
    counted, and once it is released (or a later day joins it) the same
    aggregation picks the units up. No separate carry-over bookkeeping.
    """
    return units_by_asin(days, SHIPPABLE_STATUSES)


def packed_units_by_asin(days: Sequence) -> dict[str, int]:
    """Units per ASIN across ALL days, including held ones.

    Drives the "remaining to pack" figure. Held units are counted here on
    purpose — they are already in boxes and must not be packed twice.
    """
    return units_by_asin(days, None)


def verified_units_by_asin(days: Sequence) -> dict[str, int]:
    """Units per ASIN on days the owner has verified.

    Narrower than `shippable`: a submitted day may ship, but only a verified one
    may be invoiced, because that approval is what gates the GST number. The
    shipment file's `mode=verified` and the invoice bridge both key off this.
    """
    return units_by_asin(days, INVOICEABLE_STATUSES)


def held_totals(days: Sequence) -> dict[str, int]:
    """Summary of what is parked, so a held day cannot quietly become lost stock."""
    units = cartons = day_count = 0
    for day in days:
        status = day.get("status") if isinstance(day, Mapping) else getattr(day, "status", None)
        if status != STATUS_HELD:
            continue
        day_count += 1
        entries = day.get("entries") if isinstance(day, Mapping) else getattr(day, "entries", [])
        units += packed_units(entries or [])
        cartons += packed_cartons(entries or [])
    return {"days": day_count, "units": units, "cartons": cartons}
