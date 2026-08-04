"""Pure shipment-planning rules. No FastAPI, no database, no I/O.

Every number the shipment feature decides is computed here, so the dashboard,
the four downloads and the invoice bridge cannot disagree with each other. If
you find yourself rounding or sorting anywhere else, move it in here instead.

Vocabulary, because two of these are easy to conflate:

  planned     what the plan says to send to Amazon for a SKU
  available   finished stock already in the warehouse, ready to pack
  packed      units physically boxed, INCLUDING days that are on hold
  shippable   units on days cleared to ship, EXCLUDING held days
  remaining   planned - packed            (still to BOX — the packer's number)
  to source   planned - available - packed (still to MAKE — the owner's number)
  carry-over  the accumulated held days, judged together rather than singly

`packed` and `shippable` must stay separate. A held day's units exist in the
warehouse — telling the floor to pack them again would double-pack the order —
but they must not appear in a shipment until the day is released.

`is_held` and `carry_over` answer different questions and both are needed.
`is_held` asks "is this one day worth shipping alone?", which must stay
per-day or a small Tuesday would ship small. `carry_over` asks "is the parked
backlog worth shipping now?", which is the actual instruction behind
requirement 9 and cannot be derived from any single day.
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

# ─── Sort priority ───────────────────────────────────────────────────────────

#: Brand order on the packing sheet. Mithila Foods is the bigger line and is
#: packed first. Persisted as a rank because the stored codes are 'MF' and 'HF'
#: and those cannot order alphabetically — H sorts before M.
BRAND_ORDER = {"MF": 0, "MITHILA FOODS": 0, "HF": 1, "HOWRAH FOODS": 1}
UNKNOWN_BRAND_RANK = 2

#: Requested category order: P1 Sattu, P2 Chana, P3 Flours, P4 Rice, P5 Seeds,
#: P6 everything else.
CATEGORY_LABELS = {
    1: "Sattu",
    2: "Chana",
    3: "Flours",
    4: "Rice",
    5: "Seeds",
    6: "Rest",
}
DEFAULT_CATEGORY = 6

#: Ordered rules — FIRST match wins, so the order of this list is the rule, not
#: an implementation detail. Nine of the 74 real product names match more than
#: one keyword, and three of those change bucket depending on which is tested
#: first:
#:
#:   "Bangla Chana Sattu"  sattu + chana  -> P1, because it IS a sattu
#:   "Rice Atta"           atta  + rice   -> P3, because it IS a flour
#:   "chana dal badi"      chana + dal    -> P2, the chana is the ingredient
#:
#: Defaults only. Every one of these is overridable per product in the app, so a
#: wrong guess here is a dropdown away from fixed rather than a code change.
CATEGORY_RULES: tuple[tuple[int, tuple[str, ...]], ...] = (
    (1, ("sattu",)),
    # Flours before rice so "Rice Atta" lands in flours, and before chana so
    # "Moringa Besan" is a flour rather than a pulse.
    (3, ("atta", "besan", "flour", "maida", "suji", "sooji", "powder")),
    (2, ("chana", "chickpea", "chholay", "cholay")),
    (4, ("rice", "chawal", "moori", "chuda", "chura", "poha", "murmura", "lai")),
    (5, ("seed", "til ", "tilkut", "revdi", "flax", "posta", "sesame")),
)


def category_for(name) -> int:
    """Default sort priority 1..6 for a parent product name.

    Keyword-derived, first rule wins. Returns 6 ("Rest") for anything unmatched,
    which is the requested behaviour rather than a failure — most of the
    catalogue's 74 products are legitimately "rest".

    Substring matching is deliberate over word matching: the names in
    product_families.json are inconsistently spaced and cased ("bss 200g",
    "Desi Tilkut Jaggery", "banskathi rice"), so requiring word boundaries would
    miss more than it would protect. The one place that bites is short keys, so
    "til " carries its trailing space to avoid matching "Tilkut" via "til" and,
    worse, "utility"-style false hits in future names.
    """
    text = f" {str(name or '').casefold().strip()} "
    if not text.strip():
        return DEFAULT_CATEGORY
    for priority, keywords in CATEGORY_RULES:
        if any(keyword in text for keyword in keywords):
            return priority
    return DEFAULT_CATEGORY


def brand_rank_for(brand) -> int:
    """0 for Mithila Foods, 1 for Howrah Foods, 2 for anything unrecognised.

    Unknown brands sort last rather than first: a new brand appearing at the top
    of the packing sheet would look like the sheet was broken, whereas at the
    bottom it looks like what it is — something new to classify.
    """
    return BRAND_ORDER.get(str(brand or "").strip().upper(), UNKNOWN_BRAND_RANK)


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


def _int_of(item, name, default):
    raw = item.get(name) if isinstance(item, Mapping) else getattr(item, name, None)
    if raw is None:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def sort_key(item) -> tuple:
    """Brand → category → product → weight → ASIN.

        Mithila Foods before Howrah Foods
        P1 Sattu, P2 Chana, P3 Flours, P4 Rice, P5 Seeds, P6 Rest
        then product name, then weight, then ASIN

    **Product sits above weight on purpose.** It means every size of one product
    is a contiguous block ("ABC Sattu 0.5kg, ABC Sattu 1kg, Beetroot Sattu…"),
    so the packer picks one product from one warehouse location and finishes it.
    Weight-above-product would group all 0.5kg pouches together instead and
    scatter each product down the page.

    Reads pre-computed ranks (`brand_rank`, `category_rank`) when they are
    present, falling back to deriving them from `brand` and the product name.
    The fallback is what lets this function work on plain dicts in tests and on
    rows loaded before the ranks existed; the persisted/joined values are what
    let SQL reproduce this exact order in ``repository.load_plan_items``.

    Casefolded so ordering does not depend on how a name happens to be
    capitalised in product_families.json ('sattu' vs 'Jau Sattu'). ASIN is the
    final tiebreak purely for determinism — two SKUs with the same product and
    weight would otherwise sort arbitrarily, and the screen and a download could
    then disagree.

    Accepts either a dict or an ORM object, so callers can use it before or
    after persistence.
    """
    product = _text_of(item, "sort_product", "item", "parent_product")

    brand_rank = _int_of(item, "brand_rank", None)
    if brand_rank is None:
        brand_rank = brand_rank_for(_text_of(item, "brand"))

    category_rank = _int_of(item, "category_rank", None)
    if category_rank is None:
        category_rank = category_for(product)

    return (
        brand_rank,
        category_rank,
        product.casefold(),
        _weight_of(item),
        _text_of(item, "asin"),
    )


def sort_items(items: Iterable) -> list:
    """Brand-then-category-then-product-then-weight. The only ordering anywhere.

    ``repository.load_plan_items`` must reproduce this in SQL;
    tests/test_shipment_plan_db.py asserts the two agree, which is what stops the
    screen and the five downloads drifting apart.
    """
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


def _as_count(value) -> int:
    """A non-negative integer from anything the CSV or a form might hand us."""
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def remaining_for(planned, packed: int) -> int:
    """How many units still need boxing — the PACKER's question.

    ``available`` is deliberately not a parameter. Stock sitting finished on a
    warehouse shelf still has to be put into cartons, so subtracting it here
    would tell the packer to box less than the plan needs and the shipment would
    go out short. See ``still_to_source`` for the owner's different question.

    Over-packing clamps to 0 rather than showing a negative: the warehouse sheet
    should read "nothing left to do", not "-40 to pack".
    """
    return max(0, _as_count(planned) - _as_count(packed))


def still_to_source(planned, packed: int, available=0) -> int:
    """How much must still be produced or bought — the OWNER's question.

    Two numbers, two names, for the same reason ``packed`` and ``shippable`` are
    separate: they answer different questions and collapsing them hides a real
    error. The packer needs to know what to box; the owner needs to know what to
    make. Stock already on the shelf changes the second and not the first.

        still_to_source(610, 0, 0)     == 610
        still_to_source(610, 0, 200)   == 410   # 200 already on the shelf
        still_to_source(610, 0, 700)   == 0     # fully covered by stock on hand
        still_to_source(610, 100, 200) == 310   # and 100 of it is already boxed

    This function exists because ``available`` was a bug, not a gap. It has been
    an editable column since the first Shipment build (commit 12cb580) and never
    fed a single calculation in any renderer or document — typing into it changed
    nothing on screen, which reads as the page being broken.
    """
    return max(0, _as_count(planned) - _as_count(available) - _as_count(packed))


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


def held_days(days: Sequence) -> list:
    """Just the days that are parked, in the order they were given.

    Callers get the dates so a held day can be named rather than merely counted:
    "2 days on hold" is not actionable, "30 Jul and 31 Jul are on hold" is.
    """
    out = []
    for day in days:
        status = day.get("status") if isinstance(day, Mapping) else getattr(day, "status", None)
        if status == STATUS_HELD:
            out.append(day)
    return out


def held_totals(days: Sequence) -> dict[str, int]:
    """Summary of what is parked, so a held day cannot quietly become lost stock."""
    units = cartons = 0
    parked = held_days(days)
    for day in parked:
        entries = day.get("entries") if isinstance(day, Mapping) else getattr(day, "entries", [])
        units += packed_units(entries or [])
        cartons += packed_cartons(entries or [])
    return {"days": len(parked), "units": units, "cartons": cartons}


def carry_over(
    days: Sequence,
    min_cartons: int = DEFAULT_MIN_CARTONS,
    min_units: int = DEFAULT_MIN_UNITS,
) -> dict:
    """The parked backlog, and whether it is now big enough to ship.

    This is the half of requirement 9 that `is_held` cannot answer. `is_held`
    judges one day **in isolation**, which is correct — a 20-carton day genuinely
    should not become its own shipment, and a later day must be held too rather
    than shipping small on its own. But that leaves the actual instruction
    ("combine it with next day packing and then create a shipment") with no
    trigger: Monday is held, Tuesday is held, together they clear the minimum,
    and nothing anywhere says so. Held stock then accumulates until somebody
    happens to look, which is exactly how a held day becomes lost stock.

    So the accumulated total is checked against the same thresholds:

        Mon 20c/400u held  +  Tue 10c/200u held  ->  30c/600u, clears -> ship
        Mon 20c/400u held  +  Tue  2c/40u  held  ->  22c/440u, still short

    `clears` is a prompt, never an action. Releasing stays the owner's decision
    (a big backlog may still be worth holding for a fuller truck), so nothing
    here changes a status — it only makes the situation impossible to miss.

    `shortfall_*` is what is still needed on each axis, so a screen can say
    "5 more cartons or 100 more units" instead of only "not yet".
    """
    totals = held_totals(days)
    units = int(totals["units"])
    cartons = int(totals["cartons"])
    floor_cartons = max(0, int(min_cartons or 0))
    floor_units = max(0, int(min_units or 0))

    # `is_held` treats an empty day as "not held" rather than "held", so guard on
    # the day count: zero parked days must not read as a backlog that clears.
    clears = totals["days"] > 0 and not is_held(cartons, units, floor_cartons, floor_units)

    return {
        "days": totals["days"],
        "dates": [
            (d.get("pack_date") if isinstance(d, Mapping) else getattr(d, "pack_date", ""))
            or ""
            for d in held_days(days)
        ],
        "units": units,
        "cartons": cartons,
        "clears": clears,
        "min_cartons": floor_cartons,
        "min_units": floor_units,
        "shortfall_cartons": max(0, floor_cartons - cartons),
        "shortfall_units": max(0, floor_units - units),
    }
