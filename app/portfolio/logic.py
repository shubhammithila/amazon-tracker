"""Pure rules for the Portfolio tab. No database, no network.

Joins three sources that each know something the others do not:

* **Amazon's economics** (per child ASIN): sales, refunds, fees, ad spend, net proceeds.
* **The live MRP sheet** (``app.shipment.catalogue``): the product NAME, brand and pack
  weight. Verified: all 267 selling child ASINs are present in the sheet's 271, and every
  Amazon ``parentAsin`` maps to exactly one catalogue name — so parent names come from here
  rather than from the stale ``app/invoice/product_families.json`` the old tab read.
* **Our own review scraper** (``rating_history``): the star rating and review count.

**Ratings belong to the PARENT, and that is measured, not assumed.** Amazon pools reviews
across a variation family: Roasted Chana 1 kg, 1.5 kg and 2 kg all report 4.2 stars from 477
reviews — the same numbers, because they are one listing family. Confirmed independently from
two directions: the 261 rated ASINs carry exactly **90 distinct (rating, count) pairs**, and
the economics API reports exactly **90 parent ASINs**. The two sources agree on the family
structure without being told to.

The consequence is a rule about precision: **a rating cannot discriminate between pack
sizes**, so it is shown on the parent row only. A per-size verdict is decided on economics
alone, which genuinely does differ per size — the measured pack-size tax is 500 g and under
running 48% TACOS for 14.8% net, against 1 kg and over at 24% for 39.0%.

**Every margin here is PRE-COGS.** ``netProceeds`` is Amazon's figure: sales minus Amazon's
fees minus ads. It does not include what it costs to make the product, because Amazon does not
know that unless the seller enters it in Seller Central (the schema's ``cost`` field, currently
null on this account). So a size showing +8.8% may still lose money, and the screen says so
rather than implying a precision the input does not have.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date

# ─── Verdicts ────────────────────────────────────────────────────────────────
#
# Named rather than scored. A 0-100 composite would rank the portfolio in one column, but a
# ranking cannot be argued with: "score 23" gives the owner nothing to check, while
# "net -56.8%, TACOS 78%" is a claim he can verify against Seller Central and overrule.

VERDICT_DEAD = "DEAD"           # no volume, so no signal — just cost
VERDICT_KILL = "KILL"           # losing money, or being returned
VERDICT_SURGICAL = "SURGICAL"   # the parent works, some sizes do not
VERDICT_BEST_BET = "BEST BET"   # profitable, cheap to advertise, well reviewed
VERDICT_SCALE = "SCALE"         # profitable and cheap, but the reviews are a problem
VERDICT_MONITOR = "MONITOR"     # everything else

#: Display order for the verdict summary strip: worst first, because the strip is a worklist
#: and the killable products are what the owner opened the tab to find.
VERDICT_ORDER = (
    VERDICT_KILL, VERDICT_SURGICAL, VERDICT_DEAD,
    VERDICT_MONITOR, VERDICT_SCALE, VERDICT_BEST_BET,
)

#: Below this many net units in the window there is no signal to read. Two, not zero: a
#: single unit that was then refunded produced a 154% TACOS and a -71.7% margin in the real
#: data, and calling that "the worst product in the portfolio" would be reading noise.
DEAD_UNITS = 2

#: A return rate this high on real volume is a PRODUCT problem, not a pricing one, and money
#: cannot fix it. Escalates on its own for that reason — a flattering margin on a product a
#: sixth of buyers send back is not a keeper.
RETURNS_KILL_RATE = 0.15
RETURNS_MIN_UNITS = 20

#: Net margin at or above this is genuinely healthy on this account: measured, the 1 kg and
#: larger packs run 39.0% while the whole account averages 28.9%.
GOOD_NET = 0.25

#: Ad dependence at or below this is efficient here. The account average is 33.2%, and the
#: profitable large packs sit at 16-29%.
GOOD_TACOS = 0.30

#: Losing money AND heavily ad-dependent. Both, not either: a negative margin at low TACOS is
#: a pricing problem worth fixing, while a negative margin sustained by heavy spend is a
#: product being bought only because it is being paid for.
KILL_TACOS = 0.50

#: A rating below this is a product problem worth fixing before spending more on it. 4.0 is
#: where Amazon shoppers visibly hesitate, and every product in the first kill list sat at
#: 3.7-3.9.
GOOD_RATING = 4.0


def _num(value) -> float:
    """A float from anything Amazon or SQLAlchemy hands over. Never raises.

    Defensive because three shapes arrive at this module: Amazon's nested ``{"amount": ...}``
    dicts, SQLAlchemy's ``Decimal`` for ``Numeric`` columns, and ``None`` for absent data. A
    ``TypeError`` here would blank the whole dashboard over one missing fee.
    """
    if value is None:
        return 0.0
    if isinstance(value, Mapping):
        value = value.get("amount")
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _ratio(part: float, whole: float) -> float | None:
    """``part / whole``, or **None** when there is no denominator.

    None rather than 0.0, and the distinction reaches the screen: a product with no sales has
    no TACOS, and rendering that as "0%" would place it among the most ad-efficient products
    in the portfolio. The template prints an em dash for None.
    """
    if not whole:
        return None
    return part / whole


def size_row(econ: Mapping, catalogue: Mapping) -> dict:
    """One child ASIN — a pack size — with its own economics.

    The pack size is where a kill decision is actually taken, which is why this is the grain
    the API is queried at. Weight and name come from the live MRP sheet; an ASIN the sheet has
    never heard of is kept and FLAGGED rather than dropped, because a product missing from a
    portfolio review is a product nobody reviews.
    """
    asin = (econ.get("childAsin") or "").strip().upper()
    entry = catalogue.get(asin) or {}
    sales = econ.get("sales") or {}

    ordered = _num(sales.get("orderedProductSales"))
    refunded = _num(sales.get("refundedProductSales"))
    units = int(sales.get("netUnitsSold") or 0)
    units_ordered = int(sales.get("unitsOrdered") or 0)
    units_refunded = int(sales.get("unitsRefunded") or 0)

    fees = {}
    for fee in econ.get("fees") or []:
        name = fee.get("feeTypeName") or "Other"
        total = sum(
            _num((charge.get("aggregatedDetail") or {}).get("totalAmount"))
            for charge in (fee.get("charges") or [])
        )
        # Amazon reports fees as CHARGES (positive numbers that reduce proceeds). Stored as
        # given, with the sign convention stated once here rather than guessed at each use.
        fees[name] = round(fees.get(name, 0.0) + total, 2)

    ads = 0.0
    ad_types = {}
    for ad in econ.get("ads") or []:
        amount = _num((ad.get("charge") or {}).get("totalAmount"))
        ads += amount
        ad_types[ad.get("adTypeName") or "Other"] = round(amount, 2)

    net = _num((econ.get("netProceeds") or {}).get("total"))

    return {
        "asin": asin,
        "parent_asin": (econ.get("parentAsin") or "").strip().upper(),
        "product": entry.get("name") or "",
        "brand": entry.get("brand") or "",
        "weight": float(entry.get("weight") or 0),
        "known": bool(entry),
        "sales": round(ordered, 2),
        "refunded": round(refunded, 2),
        "units": units,
        "units_ordered": units_ordered,
        "units_refunded": units_refunded,
        "ad_spend": round(ads, 2),
        "ad_types": ad_types,
        "fees": fees,
        "fees_total": round(sum(fees.values()), 2),
        "net": round(net, 2),
        "net_pct": _ratio(net, ordered),
        "tacos": _ratio(ads, ordered),
        "returns_pct": _ratio(units_refunded, units_ordered),
    }


def verdict_for(row: Mapping, *, rating: float | None, sizes: Sequence[Mapping] = ()) -> tuple:
    """``(verdict, reason)`` for one parent product.

    **The reason travels with the verdict** so the screen can show the numbers that produced
    it. A verdict the owner cannot audit is a verdict he has to either trust blindly or
    ignore, and both are worse than an argument.

    **Rule ORDER is part of the rule**, and each early return states why it outranks what
    follows. Reordering these silently changes conclusions, which is why a test asserts the
    order rather than only the individual rules.
    """
    units = int(row.get("units") or 0)
    net_pct = row.get("net_pct")
    tacos = row.get("tacos")
    returns_pct = row.get("returns_pct")
    units_ordered = int(row.get("units_ordered") or 0)

    # FIRST: no volume, no signal. Ahead of everything because the percentages computed from
    # one or two units are arithmetic on noise — a single refunded unit yields -71.7% net and
    # 154% TACOS, and both "kill this" and "best bet" would be unfounded.
    if units <= DEAD_UNITS:
        return VERDICT_DEAD, f"only {units} unit(s) sold in the window — too little to judge"

    # SECOND: returns, ahead of the money rules. A product a sixth of buyers send back has a
    # problem that better pricing or cheaper ads cannot fix, and its margin may look fine
    # because refunds are already netted off.
    if (
        returns_pct is not None
        and returns_pct >= RETURNS_KILL_RATE
        and units_ordered >= RETURNS_MIN_UNITS
    ):
        return VERDICT_KILL, (
            f"{returns_pct * 100:.0f}% of {units_ordered} units were returned — "
            "a product problem, not a pricing one"
        )

    if net_pct is None:
        return VERDICT_DEAD, "no sales in the window"

    # THIRD: losing money AND ad-dependent.
    if net_pct < 0 and tacos is not None and tacos > KILL_TACOS:
        return VERDICT_KILL, (
            f"net {net_pct * 100:.1f}% with {tacos * 100:.0f}% TACOS — "
            "losing money on ads that are buying the sales"
        )

    # FOURTH: the parent earns its place but some sizes do not. Checked BEFORE the positive
    # verdicts, because a parent averaging +6.3% can hide a 0.5 kg pack at -30% — which is
    # exactly the Beetroot Sattu shape, and the reason the whole tab expands to sizes.
    losers = [s for s in sizes if (s.get("net_pct") or 0) < 0 and int(s.get("units") or 0) > DEAD_UNITS]
    if net_pct > 0 and losers:
        names = ", ".join(_size_label(s) for s in losers[:3])
        return VERDICT_SURGICAL, (
            f"the product earns {net_pct * 100:.1f}% overall, but {len(losers)} size(s) "
            f"lose money ({names}) — kill those, keep the rest"
        )

    if net_pct >= GOOD_NET and tacos is not None and tacos <= GOOD_TACOS:
        if rating is not None and rating < GOOD_RATING:
            return VERDICT_SCALE, (
                f"net {net_pct * 100:.1f}% at {tacos * 100:.0f}% TACOS, but only "
                f"{rating:.1f} stars — fix the product, then spend more"
            )
        return VERDICT_BEST_BET, (
            f"net {net_pct * 100:.1f}% at {tacos * 100:.0f}% TACOS"
            + (f" and {rating:.1f} stars" if rating is not None else "")
        )

    parts = [f"net {net_pct * 100:.1f}%"]
    if tacos is not None:
        parts.append(f"TACOS {tacos * 100:.0f}%")
    if rating is not None:
        parts.append(f"{rating:.1f} stars")
    return VERDICT_MONITOR, ", ".join(parts)


def _size_label(size: Mapping) -> str:
    """A pack size named for a human: "500 g" if the weight is known, else the ASIN."""
    from app.shipment.logic import weight_label

    weight = float(size.get("weight") or 0)
    return weight_label(weight) if weight else (size.get("asin") or "?")


def portfolio(
    econ_rows: Sequence,
    catalogue: Mapping,
    ratings: Mapping,
    decisions: Mapping | None = None,
    today: date | None = None,
) -> dict:
    """The whole dashboard: parent products, their sizes, verdicts and totals.

    ONE function behind the screen, the Excel export and every test, so a printed row cannot
    disagree with the monitor about a margin — the same reasoning the shipment feature's
    single ``_document_rows`` carries.

    **A parent's money is the SUM of its sizes, never a separate parent-level query.** Amazon
    would answer a ``PARENT_ASIN`` query directly, and using it would create two numbers for
    one thing that could drift apart; summing means the expanded rows always add up to the row
    above them. This is the same defect class as the Orders tab reporting 86 orders beside 87
    lines.
    """
    decisions = decisions or {}
    by_parent: dict[str, dict] = {}
    unmatched: set[str] = set()

    for econ in econ_rows:
        size = size_row(econ, catalogue)
        if not size["asin"]:
            continue
        if not size["known"]:
            unmatched.add(size["asin"])

        parent_asin = size["parent_asin"] or size["asin"]
        parent = by_parent.setdefault(parent_asin, {
            "parent_asin": parent_asin, "sizes": [],
            "product": "", "brand": "",
        })
        parent["sizes"].append(size)
        # The catalogue agrees on the name across a family (measured: one name per parent), so
        # the first non-empty one names the row. Falls back to the ASIN rather than "Unknown",
        # which at least identifies the listing.
        if not parent["product"] and size["product"]:
            parent["product"] = size["product"]
            parent["brand"] = size["brand"]

    parents = []
    for parent_asin, parent in by_parent.items():
        sizes = sorted(parent["sizes"], key=lambda s: (-s["sales"], s["asin"]))
        agg = _sum_sizes(sizes)
        rating_row = _rating_for(sizes, ratings)
        rating = rating_row.get("rating")

        verdict, reason = verdict_for(agg, rating=rating, sizes=sizes)
        decision = decisions.get(parent_asin) or {}
        parents.append({
            "parent_asin": parent_asin,
            "product": parent["product"] or parent_asin,
            "brand": parent["brand"],
            "rating": rating,
            "rating_count": rating_row.get("rating_count"),
            "rating_at": rating_row.get("scraped_at"),
            "verdict": verdict,
            "verdict_reason": reason,
            "decision": decision.get("decision") or "",
            "decision_note": decision.get("note") or "",
            "decision_at": decision.get("decided_at") or "",
            "sizes": sizes,
            **agg,
        })

    # Heaviest revenue first: the portfolio question is "where is the money", and a product
    # with ten times the sales deserves the first look. Name breaks the tie so two renders of
    # the same data agree.
    parents.sort(key=lambda p: (-p["sales"], p["product"].casefold()))

    totals = _sum_sizes([size for parent in parents for size in parent["sizes"]])
    counts = {verdict: 0 for verdict in VERDICT_ORDER}
    for parent in parents:
        counts[parent["verdict"]] = counts.get(parent["verdict"], 0) + 1

    return {
        "parents": parents,
        "totals": {**totals, "parents": len(parents), "verdicts": counts},
        "unmatched_asins": sorted(unmatched),
    }


def _sum_sizes(sizes: Sequence[Mapping]) -> dict:
    """Add sizes up into one set of figures, recomputing the ratios from the sums.

    **The percentages are recomputed, never averaged.** Averaging the children's TACOS would
    weight a 1-unit size equally with a 400-unit one and produce a number that belongs to no
    product — the classic error this function exists to avoid.
    """
    sales = round(sum(_num(s.get("sales")) for s in sizes), 2)
    ads = round(sum(_num(s.get("ad_spend")) for s in sizes), 2)
    net = round(sum(_num(s.get("net")) for s in sizes), 2)
    fees_total = round(sum(_num(s.get("fees_total")) for s in sizes), 2)
    units = sum(int(s.get("units") or 0) for s in sizes)
    units_ordered = sum(int(s.get("units_ordered") or 0) for s in sizes)
    units_refunded = sum(int(s.get("units_refunded") or 0) for s in sizes)

    fees: dict[str, float] = {}
    for size in sizes:
        for name, amount in (size.get("fees") or {}).items():
            fees[name] = round(fees.get(name, 0.0) + _num(amount), 2)

    return {
        "sales": sales,
        "ad_spend": ads,
        "net": net,
        "fees_total": fees_total,
        "fees": fees,
        "units": units,
        "units_ordered": units_ordered,
        "units_refunded": units_refunded,
        "net_pct": _ratio(net, sales),
        "tacos": _ratio(ads, sales),
        "returns_pct": _ratio(units_refunded, units_ordered),
    }


def _rating_for(sizes: Sequence[Mapping], ratings: Mapping) -> dict:
    """The family's rating, taken from whichever size we last scraped.

    **Any size answers for the family**, because Amazon pools reviews across a variation
    family — measured, all sizes of one product report identical rating and count. The most
    recently scraped one is preferred so a partial scrape still yields the freshest figure
    rather than an arbitrary one.
    """
    best: dict = {}
    for size in sizes:
        row = ratings.get(size.get("asin")) or {}
        if row.get("rating") is None:
            continue
        if not best or str(row.get("scraped_at") or "") > str(best.get("scraped_at") or ""):
            best = row
    return best
