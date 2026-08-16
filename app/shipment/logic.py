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
  cartons     boxes packed on a DAY. Not per SKU — a carton holds whatever was
              being packed when it was filled, so it belongs to several ASINs at
              once. Everything else in this list is per-SKU; this one is not, and
              that asymmetry is deliberate rather than an oversight.

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


def line_weight(units, pack_weight) -> float:
    """Net weight of one invoice line: units × pack size, in kg.

    Rounded to 3 decimals because the inputs are pack sizes like 0.25 and 0.15 and
    float multiplication produces 30.000000000000004, which then renders on a GST
    document. Three places keeps a 50 g pack meaningful while hiding the noise.
    """
    return round(_as_count(units) * max(0.0, _as_float(pack_weight)), 3)


def shipment_weight(lines) -> dict:
    """Total NET weight of a shipment, plus the working.

    ``lines`` is any iterable of mappings with ``quantity`` (or ``units``) and
    ``weight`` (the per-unit pack size in kg). Returns::

        {"total": 130.5, "lines": [{...}], "unknown": 2, "counted": 8}

    **Net, not gross, and that distinction is on the label wherever this is shown.**
    It is the weight of the product only — cartons, filler and tape are not in the
    catalogue, so the figure a transporter weighs will be higher. Silently presenting
    this as the shipment weight would put a number on an invoice that disagrees with
    the weighbridge, and nobody would know which to trust.

    ``unknown`` counts lines whose pack size is missing or zero, and those are excluded
    from the total rather than treated as 0 kg. A line silently contributing nothing is
    how a 130 kg shipment reports 90 kg — the caller surfaces the count so the total is
    never quietly short.

    Returned as a breakdown rather than a bare float so the screen can show the working.
    A weight the owner cannot check is one he has to either trust blindly or ignore, and
    on a document that goes to a transporter he will ignore it.
    """
    detail = []
    total = 0.0
    unknown = 0

    for line in lines or []:
        if not isinstance(line, Mapping):
            continue
        units = _as_count(line.get("quantity", line.get("units", 0)))
        if units <= 0:
            continue

        pack = _as_float(line.get("weight"))
        if pack <= 0:
            unknown += 1
            detail.append({
                "title": str(line.get("title") or line.get("item") or line.get("asin") or ""),
                "units": units,
                "pack_weight": None,
                "weight": None,
            })
            continue

        weight = line_weight(units, pack)
        total += weight
        detail.append({
            "title": str(line.get("title") or line.get("item") or line.get("asin") or ""),
            "units": units,
            "pack_weight": pack,
            "pack_label": weight_label(pack),
            "weight": weight,
        })

    return {
        "total": round(total, 3),
        "lines": detail,
        "unknown": unknown,
        "counted": len(detail) - unknown,
    }


def _as_float(value) -> float:
    """A float from anything the CSVs or JSON can produce. 0.0 on nonsense.

    Separate from ``_as_count`` because a pack size is fractional (0.25 kg) where a
    unit count never is, and rounding a pack size to an int would turn every gram
    figure into zero.
    """
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def weight_label(weight) -> str:
    """Pack size as the warehouse says it: grams under 1 kg, kilos at 1 kg and up.

        0.25 -> "250g"       1.0  -> "1 kg"
        0.5  -> "500g"       1.05 -> "1.05 kg"
        0.15 -> "150g"       2.0  -> "2 kg"

    "0.5kg" is arithmetically identical and wrong on a picking sheet — nobody in a
    warehouse says "zero point five kilo", they say "five hundred gram", and the
    label on the pouch says 500g. A packer scanning 100 rows should not be doing
    unit conversion in his head.

    **The spacing differs between the two units on purpose, because it is what the
    owner reads as correct**: grams close up ("500g"), kilos spaced ("1 kg"). It
    matches how the pouches are labelled, and a column of "500 g" had the unit
    floating far enough from its number to scan as a separate cell.

    The threshold is at 1 kg and not, say, 250 g because that is the boundary the
    printed labels use.

    Grams are shown as integers where they are whole (they always are in this
    catalogue — 0.1 to 0.9 in 50 g steps) but a stray 0.125 would render "125g"
    rather than being silently rounded. Kilos keep up to two decimals because 1.05
    is a real pack size here, and trailing zeros are trimmed so a 1 kg bag does
    not read "1.00 kg".

    Lives here rather than in documents.py or a template because three places
    render weight — the owner's table, the packer's screen and the documents — and
    they were each doing it differently.
    """
    try:
        value = float(weight or 0)
    except (TypeError, ValueError):
        return ""
    if value <= 0:
        # Blank, not "0 kg". A zero weight on a warehouse sheet reads as a real
        # fact about the product and sends someone looking for the 0 kg bag.
        return ""

    if value < 1:
        grams = value * 1000
        text = f"{grams:.2f}".rstrip("0").rstrip(".")
        return f"{text}g"

    text = f"{value:.2f}".rstrip("0").rstrip(".")
    return f"{text} kg"


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


def day_cartons(day) -> int:
    """The cartons packed on one day.

    **Read off the DAY, never summed from its entries.** A carton is filled with
    whatever is being packed at the time, so a mixed box belongs to several ASINs
    and to none of them — "carton is not item wise. it is random. like 500 units
    packed today in 20 cartons."

    This replaced ``packed_cartons(entries)``, which summed a per-entry field. That
    field asked the packer a question he could not answer, so it was guessed or left
    blank, and the guess prefilled the Boxes field on a GST invoice.
    """
    raw = (
        day.get("total_cartons")
        if isinstance(day, Mapping)
        else getattr(day, "total_cartons", 0)
    )
    return _as_count(raw)


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

    **That clamp is why ``over_packed`` exists.** Clamping is right for this number
    and it also makes an over-pack invisible here, so the excess is reported
    separately rather than by un-clamping this one.
    """
    return max(0, _as_count(planned) - _as_count(packed))


def over_packed(planned, packed: int) -> int:
    """Units boxed BEYOND the plan, or 0. The complement of ``remaining_for``.

    Exists because ``remaining_for`` clamps at 0, which is correct for a to-do
    number and silently hides the opposite error: a plan of 50 with 100 packed reads
    exactly like a plan of 50 fully finished. Both screens showed "0 to pack" and
    nothing else.

    It is worth a number of its own rather than a negative "to pack", because the
    two are acted on differently and by different people:

    * the packer needs to stop and be told, before he boxes more of it;
    * the owner needs to decide, because the surplus is real stock and the invoice
      will bill the packed quantity, not the planned one — the boxes exist and they
      are going to Amazon, so the choice is to raise To Ship to match or to unpack.

    A typo is the common cause (500 for 50), and it reaches a GST invoice through
    the invoice bridge, which aggregates what was PACKED.
    """
    return max(0, _as_count(packed) - _as_count(planned))


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
    """Summary of what is parked, so a held day cannot quietly become lost stock.

    Units come from the entries (they are per-SKU and must agree with what the packer
    typed against each row); cartons come from the day (they are not per-SKU at all).
    Two different sources on purpose — see ``day_cartons``.
    """
    units = cartons = 0
    parked = held_days(days)
    for day in parked:
        entries = day.get("entries") if isinstance(day, Mapping) else getattr(day, "entries", [])
        units += packed_units(entries or [])
        cartons += day_cartons(day)
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


# ─── The Amazon inbound-plan body ─────────────────────────────────────────────
#
# Pure, and deliberately so: this builds the request that would create a REAL shipment
# at Amazon, and the one thing that must be provable without a network call is that it
# refuses to send an incomplete one.

#: Amazon's inbound plan keys every line on the merchant SKU (`msku`), so a line without
#: one cannot be sent at all. Verified against the live account: our `fba_sku` values
#: ("abc_sattu500g FBA") are exactly the shape Amazon returns ("wss 200g FBA").
def amazon_plan_items(items, units_by_asin) -> dict:
    """Split plan items into what Amazon can accept and what blocks the shipment.

    Returns ``{"lines": [...], "missing_sku": [...], "units": int}``.

    ``units_by_asin`` is what was actually PACKED on the chosen days — not the plan. A
    shipment must describe the boxes that exist, and those two numbers differ whenever
    the packer over- or under-packed against the plan.

    **A line with no merchant SKU is separated out, never silently dropped.** Amazon
    keys on the msku, so sending such a line either fails the whole request or, worse,
    is accepted with that line absent — real stock in a real carton that the shipment
    does not mention, discovered at the FC. The caller refuses the whole shipment and
    names the products, because the fix is one field in the plan and the alternative is
    a physical reconciliation.

    A zero-unit line is skipped in silence: nothing was packed, so there is nothing to
    declare and it is not a problem to report.
    """
    lines: list[dict] = []
    missing_sku: list[dict] = []
    total = 0

    for item in items or []:
        asin = (getattr(item, "asin", "") or "").strip()
        units = _as_count((units_by_asin or {}).get(asin, 0))
        if units <= 0:
            continue

        sku = (getattr(item, "fba_sku", "") or "").strip()
        label = (getattr(item, "item", "") or "").strip() or asin
        weight_text = weight_label(getattr(item, "weight", 0))

        if not sku:
            missing_sku.append({
                "asin": asin,
                "item": label,
                "pack_size": weight_text,
                "units": units,
            })
            continue

        total += units
        lines.append({
            "msku": sku,
            "quantity": units,
            # `labelOwner: SELLER` but `prepOwner: NONE`, and the asymmetry is not a
            # typo — it is what Amazon actually accepts.
            #
            # Every existing plan REPORTS `prepOwner: SELLER`, so that is what this sent
            # originally, and creating a plan with it was rejected outright:
            #
            #   400 ERROR: abc_sattu500g FBA does not require prepOwner but SELLER was
            #              assigned. Accepted values: [NONE]
            #
            # A value Amazon RETURNS is not necessarily one it ACCEPTS. The error names
            # the msku, so this is per-SKU: a product that genuinely needed prep would
            # want SELLER, and Amazon says so by name rather than failing silently.
            "labelOwner": "SELLER",
            "prepOwner": "NONE",
            # Not sent to Amazon — carried so the dry run can be READ by a human.
            # A screen of mskus and numbers is not checkable; product names are.
            "_item": label,
            "_asin": asin,
            "_pack_size": weight_text,
        })

    return {"lines": lines, "missing_sku": missing_sku, "units": total}


def amazon_plan_body(source_address: dict, items, units_by_asin,
                     marketplace_id: str) -> dict:
    """The full createInboundPlan request, or the reason it cannot be built.

    Returns ``{"ok": bool, "body": {...}, "missing_sku": [...], "lines": [...]}``.

    ``ok`` is False when ANY line lacks a merchant SKU, or when there is nothing to
    ship. Not a warning: the owner asked for the shipment to be blocked until the SKUs
    are filled in, and that is the right call — a shipment missing a line is discovered
    by Amazon receiving boxes it has no record of.

    The body is still returned when ``ok`` is False, so the screen can show what WOULD
    be sent alongside what is blocking it. Showing the reason without the context is how
    a refusal reads as a bug.
    """
    split = amazon_plan_items(items, units_by_asin)
    ok = bool(split["lines"]) and not split["missing_sku"]

    return {
        "ok": ok,
        "missing_sku": split["missing_sku"],
        "lines": split["lines"],
        "units": split["units"],
        "body": {
            "destinationMarketplaces": [marketplace_id],
            "sourceAddress": source_address,
            # The private `_` keys are stripped here: they exist for the dry-run screen
            # and Amazon rejects unknown fields.
            "items": [
                {k: v for k, v in line.items() if not k.startswith("_")}
                for line in split["lines"]
            ],
        },
    }
