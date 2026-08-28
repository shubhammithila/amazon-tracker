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
VERDICT_AD_DEPENDENT = "AD DEPENDENT"   # profitable, but the ads themselves lose money
VERDICT_BEST_BET = "BEST BET"   # profitable, cheap to advertise, well reviewed
VERDICT_SCALE = "SCALE"         # profitable and cheap, but the reviews are a problem
VERDICT_MONITOR = "MONITOR"     # everything else

#: Display order for the verdict summary strip: worst first, because the strip is a worklist
#: and the killable products are what the owner opened the tab to find.
VERDICT_ORDER = (
    VERDICT_KILL, VERDICT_SURGICAL, VERDICT_AD_DEPENDENT, VERDICT_DEAD,
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

#: ACOS above this means the advertising loses money on its own terms: more is spent on ads
#: than the sales they are credited with. 100% is not a tuning choice, it is break-even —
#: which is why it is the threshold rather than something like 80%.
#:
#: Measured account-wide: TACOS 33.1% against a true ACOS of **89.9%**, so Rs 1 of ads returns
#: Rs 1.11 of attributed sales. Individual products run far worse: B0GW388QP6 spends Rs 36,514
#: to earn Rs 12,815 (285%).
BREAK_EVEN_ACOS = 1.00

#: **The editable rules, and the single source of their names.**
#:
#: The values are measured from this account, not preferences — but they are still thresholds
#: someone should be able to argue with, so the dashboard can save its own set and
#: `verdict_for` takes them as a parameter. This dict is what Reset restores and what
#: `repository.save_settings` validates incoming keys against, so a typo cannot silently do
#: nothing.
DEFAULT_THRESHOLDS = {
    "dead_units": DEAD_UNITS,
    "returns_kill_rate": RETURNS_KILL_RATE,
    "returns_min_units": RETURNS_MIN_UNITS,
    "good_net": GOOD_NET,
    "good_tacos": GOOD_TACOS,
    "kill_tacos": KILL_TACOS,
    "good_rating": GOOD_RATING,
    "break_even_acos": BREAK_EVEN_ACOS,
}

#: What each rule means, in words, for the "what does KILL mean?" panel. Kept beside the
#: thresholds so an edited number and its explanation cannot drift apart — the explanation
#: names its own placeholders and the screen substitutes the live values.
VERDICT_HELP = {
    VERDICT_KILL: (
        "Losing money on advertising that is buying the sales: net margin below 0% AND "
        "TACOS above {kill_tacos:.0%}. Also fires when more than {returns_kill_rate:.0%} of "
        "at least {returns_min_units} units were returned, because that is a product problem "
        "money cannot fix."
    ),
    VERDICT_SURGICAL: (
        "The product earns its place but at least one pack size does not. Kill those sizes, "
        "keep the rest — measured, one product earned +27.1% overall while a 250 g pack burned "
        "103% TACOS for -52.7% net."
    ),
    VERDICT_AD_DEPENDENT: (
        "Profitable overall, but the ads lose money on their own terms: ACOS above "
        "{break_even_acos:.0%}, meaning more is spent on advertising than the sales Amazon "
        "credits to it. Worth cutting the spend rather than the product."
    ),
    VERDICT_DEAD: (
        "Fewer than {dead_units} net units sold in the window, so there is no signal to read. "
        "A product that sold 2 units reported +505.6% net because a refund reversal landed in "
        "the window — that is noise, not the best product in the portfolio."
    ),
    VERDICT_BEST_BET: (
        "Net margin at or above {good_net:.0%}, TACOS at or below {good_tacos:.0%}, and rated "
        "{good_rating:.1f} stars or better. Profitable, cheap to advertise, and liked."
    ),
    VERDICT_SCALE: (
        "The same economics as a best bet — net at or above {good_net:.0%} at TACOS "
        "{good_tacos:.0%} or less — but rated below {good_rating:.1f} stars. Fix the product "
        "before spending more on it."
    ),
    VERDICT_MONITOR: "Everything else: neither clearly good nor clearly bad yet.",
}


def thresholds_or_default(thresholds: Mapping | None) -> dict:
    """A complete threshold dict, filling anything absent from the measured defaults.

    Callers may pass a partial set (the settings row only stores what was edited), and a missing
    key must fall back rather than raise — a half-saved settings row would otherwise take the
    whole dashboard down.
    """
    merged = dict(DEFAULT_THRESHOLDS)
    for key, value in (thresholds or {}).items():
        if key in DEFAULT_THRESHOLDS and value is not None:
            try:
                merged[key] = float(value)
            except (TypeError, ValueError):
                continue
    return merged


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


#: Merchant SKUs on this account mark FBA with a trailing " FBA": `0.25 fc np` is the
#: merchant/Easy Ship listing, `0.25 fc np FBA` the Amazon-fulfilled one. Verified across all
#: 453 MSKU economics rows and all 213 advertised SKUs — the suffix is the only marker Amazon
#: gives at this grain.
#:
#: **This is a convention of THIS account, not an Amazon rule.** If the naming ever changes,
#: `_channel_of` is the single function to fix, which is why the check lives in one place rather
#: than being inlined wherever a channel is needed.
FBA_SKU_SUFFIX = "FBA"

CHANNEL_FBA = "fba"
CHANNEL_MERCHANT = "merchant"


def _channel_of(seller_sku) -> str:
    """Which fulfilment channel a merchant SKU belongs to. See `FBA_SKU_SUFFIX`.

    Split on whitespace rather than a substring test, so a product whose name happens to contain
    "fba" cannot be misfiled — the same care `shipment.logic.is_easy_ship` takes with its "EZ"
    token, and for the same reason.
    """
    parts = str(seller_sku or "").upper().split()
    return CHANNEL_FBA if parts and parts[-1] == FBA_SKU_SUFFIX else CHANNEL_MERCHANT


def size_row(econ: Mapping, catalogue: Mapping, ads: Mapping | None = None) -> dict:
    """One child ASIN — a pack size — with its own economics, and its ACOS when advertised.

    The pack size is where a kill decision is actually taken, which is why this is the grain
    the API is queried at. Weight and name come from the live MRP sheet; an ASIN the sheet has
    never heard of is kept and FLAGGED rather than dropped, because a product missing from a
    portfolio review is a product nobody reviews.

    `ads` is `{asin: {cost, attributed_sales, ...}}` from the Advertising API, absent when those
    credentials are not configured — in which case every ACOS is `None` and the screen says so.
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

    # `ad_spend` deliberately NOT named `ads`: the parameter of that name carries the
    # Advertising API rows, and shadowing it here silently disabled ACOS in an earlier draft.
    ad_spend = 0.0
    ad_types = {}
    for ad in econ.get("ads") or []:
        amount = _num((ad.get("charge") or {}).get("totalAmount"))
        ad_spend += amount
        ad_types[ad.get("adTypeName") or "Other"] = round(amount, 2)

    net = _num((econ.get("netProceeds") or {}).get("total"))

    # ── ACOS, from the Advertising API rather than from the economics feed ──
    #
    # `attributed_sales` is the whole reason the ads API is called: SP-API reports the ad CHARGE
    # (giving TACOS = spend / TOTAL sales) but never says which sales the ads caused. ACOS =
    # spend / ATTRIBUTED sales answers "do the ads pay for themselves", which TACOS cannot.
    #
    # The ad spend used for ACOS is the ADS API's own `cost`, not the economics `ad_spend`.
    # They reconcile to 0.2% account-wide but come from different attribution windows, and
    # dividing one source's cost by another's sales would be a ratio of two different things.
    ad_row = (ads or {}).get(asin) or {}
    attributed = _num(ad_row.get("attributed_sales")) if ad_row else 0.0
    ads_cost = _num(ad_row.get("cost")) if ad_row else 0.0

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
        "ad_spend": round(ad_spend, 2),
        "ad_types": ad_types,
        # From the Advertising API. All zero (and `acos` None) when it is not configured.
        "ads_cost": round(ads_cost, 2),
        "ad_attributed_sales": round(attributed, 2),
        "ad_clicks": int(ad_row.get("clicks") or 0),
        "ad_impressions": int(ad_row.get("impressions") or 0),
        "ad_purchases": int(ad_row.get("purchases") or 0),
        # **None means "never advertised", which is NOT 0%.** A product with no ad spend has no
        # ACOS; rendering it as 0% would make it the most efficient product in the portfolio.
        # Spend WITH zero attributed sales is a different case again — see `acos_infinite`.
        "acos": _ratio(ads_cost, attributed) if ads_cost else None,
        # Spend that produced no attributed sales at all. Measured: Rs 55,217 across 591 rows.
        # A ratio cannot express it (division by zero), so it travels as its own flag rather
        # than as a fake large number.
        "acos_infinite": bool(ads_cost and not attributed),
        # Filled by `portfolio()` from the per-SKU rows; empty when they were not fetched.
        "channels": {},
        "fees": fees,
        "fees_total": round(sum(fees.values()), 2),
        "net": round(net, 2),
        "net_pct": _ratio(net, ordered),
        # `ad_spend`, not `ads` — `ads` is the parameter holding the Advertising API rows.
        "tacos": _ratio(ad_spend, ordered),
        "returns_pct": _ratio(units_refunded, units_ordered),
    }


def verdict_for(
    row: Mapping,
    *,
    rating: float | None,
    sizes: Sequence[Mapping] = (),
    thresholds: Mapping | None = None,
) -> tuple:
    """``(verdict, reason)`` for one parent product.

    **The reason travels with the verdict** so the screen can show the numbers that produced
    it. A verdict the owner cannot audit is a verdict he has to either trust blindly or
    ignore, and both are worse than an argument.

    **Rule ORDER is part of the rule**, and each early return states why it outranks what
    follows. Reordering these silently changes conclusions, which is why a test asserts the
    order rather than only the individual rules.

    ``thresholds`` lets the owner move a line without editing code; absent, the values measured
    from this account are used. The numbers are read from the dict rather than the module
    constants so a saved setting cannot be silently ignored — a mutation that reverts to the
    constants must fail a test.
    """
    limits = thresholds_or_default(thresholds)
    dead_units = limits["dead_units"]
    units = int(row.get("units") or 0)
    net_pct = row.get("net_pct")
    tacos = row.get("tacos")
    acos = row.get("acos")
    returns_pct = row.get("returns_pct")
    units_ordered = int(row.get("units_ordered") or 0)

    # FIRST: no volume, no signal. Ahead of everything because the percentages computed from
    # one or two units are arithmetic on noise — a single refunded unit yields -71.7% net and
    # 154% TACOS, and both "kill this" and "best bet" would be unfounded.
    if units <= dead_units:
        return VERDICT_DEAD, f"only {units} unit(s) sold in the window — too little to judge"

    # SECOND: returns, ahead of the money rules. A product a sixth of buyers send back has a
    # problem that better pricing or cheaper ads cannot fix, and its margin may look fine
    # because refunds are already netted off.
    if (
        returns_pct is not None
        and returns_pct >= limits["returns_kill_rate"]
        and units_ordered >= limits["returns_min_units"]
    ):
        return VERDICT_KILL, (
            f"{returns_pct * 100:.0f}% of {units_ordered} units were returned — "
            "a product problem, not a pricing one"
        )

    if net_pct is None:
        return VERDICT_DEAD, "no sales in the window"

    # THIRD: losing money AND ad-dependent.
    if net_pct < 0 and tacos is not None and tacos > limits["kill_tacos"]:
        return VERDICT_KILL, (
            f"net {net_pct * 100:.1f}% with {tacos * 100:.0f}% TACOS — "
            "losing money on ads that are buying the sales"
        )

    # FOURTH: the parent earns its place but some sizes do not. Checked BEFORE the positive
    # verdicts, because a parent averaging +6.3% can hide a 0.5 kg pack at -30% — which is
    # exactly the Beetroot Sattu shape, and the reason the whole tab expands to sizes.
    losers = [
        s for s in sizes
        if (s.get("net_pct") or 0) < 0 and int(s.get("units") or 0) > dead_units
    ]
    if net_pct > 0 and losers:
        names = ", ".join(_size_label(s) for s in losers[:3])
        return VERDICT_SURGICAL, (
            f"the product earns {net_pct * 100:.1f}% overall, but {len(losers)} size(s) "
            f"lose money ({names}) — kill those, keep the rest"
        )

    # FIFTH: the product makes money but the ADVERTISING does not. **Only expressible with
    # attributed sales**, which is what the Advertising API added — TACOS cannot distinguish
    # "ad-dependent" from "advertised alongside strong organic sales", because its denominator
    # includes the organic sales.
    #
    # Below the positive verdicts so a profitable, efficiently-advertised product is still a
    # best bet, and above MONITOR so this does not disappear into "everything else". The action
    # differs from KILL: cut the spend, not the product.
    if net_pct > 0 and (row.get("acos_infinite") or (acos is not None and acos > limits["break_even_acos"])):
        detail = (
            "the ads produced no attributed sales at all"
            if row.get("acos_infinite") else f"ACOS {acos * 100:.0f}%"
        )
        return VERDICT_AD_DEPENDENT, (
            f"the product earns {net_pct * 100:.1f}%, but {detail} — the advertising is losing "
            "money on its own terms, so cut the spend rather than the product"
        )

    if net_pct >= limits["good_net"] and tacos is not None and tacos <= limits["good_tacos"]:
        if rating is not None and rating < limits["good_rating"]:
            return VERDICT_SCALE, (
                f"net {net_pct * 100:.1f}% at {tacos * 100:.0f}% TACOS, but only "
                f"{rating:.1f} stars — fix the product, then spend more"
            )
        return VERDICT_BEST_BET, (
            f"net {net_pct * 100:.1f}% at {tacos * 100:.0f}% TACOS"
            + (f", ACOS {acos * 100:.0f}%" if acos is not None else "")
            + (f" and {rating:.1f} stars" if rating is not None else "")
        )

    parts = [f"net {net_pct * 100:.1f}%"]
    if tacos is not None:
        parts.append(f"TACOS {tacos * 100:.0f}%")
    if acos is not None:
        parts.append(f"ACOS {acos * 100:.0f}%")
    if rating is not None:
        parts.append(f"{rating:.1f} stars")
    return VERDICT_MONITOR, ", ".join(parts)


def _size_label(size: Mapping) -> str:
    """A pack size named for a human: "500 g" if the weight is known, else the ASIN."""
    from app.shipment.logic import weight_label

    weight = float(size.get("weight") or 0)
    return weight_label(weight) if weight else (size.get("asin") or "?")


def channel_split(sku_rows: Sequence, ads_by_sku: Mapping | None = None) -> dict:
    """`{asin: {"merchant": {...}, "fba": {...}}}` from the per-SKU economics rows.

    **Shown on expand, never used for a total.** Measured on the live account: 186 of 267 child
    ASINs sell under both a merchant/Easy Ship SKU and an identically-named "… FBA" one, and the
    two sum to the ASIN figure exactly (merchant 16,68,051 + FBA 32,81,373 = 49,49,424). But
    Amazon's MSKU grain loses a little to rows it cannot attribute to a single SKU, so the
    ASIN-level rows stay authoritative and this is presentation only.

    The split is genuinely decision-relevant, which is why it is here at all: the merchant SKU of
    B0DCCL1531 spent Rs 1,444 on ads for ZERO attributed sales while its FBA twin returned 36%
    ACOS. One combined number hides that completely.
    """
    ads_by_sku = ads_by_sku or {}
    out: dict[str, dict] = {}
    for row in sku_rows:
        asin = (row.get("childAsin") or "").strip().upper()
        sku = (row.get("msku") or row.get("seller_sku") or "").strip()
        if not asin or not sku:
            continue
        channel = _channel_of(sku)
        sales = row.get("sales") or {}
        ad_row = ads_by_sku.get((asin, sku)) or {}
        ads_cost = _num(ad_row.get("cost"))
        attributed = _num(ad_row.get("attributed_sales"))

        bucket = out.setdefault(asin, {}).setdefault(channel, {
            "skus": [], "sales": 0.0, "units": 0, "net": 0.0,
            "ads_cost": 0.0, "ad_attributed_sales": 0.0,
        })
        bucket["skus"].append(sku)
        bucket["sales"] = round(bucket["sales"] + _num(sales.get("orderedProductSales")), 2)
        bucket["units"] += int(sales.get("netUnitsSold") or 0)
        bucket["net"] = round(bucket["net"] + _num((row.get("netProceeds") or {}).get("total")), 2)
        bucket["ads_cost"] = round(bucket["ads_cost"] + ads_cost, 2)
        bucket["ad_attributed_sales"] = round(bucket["ad_attributed_sales"] + attributed, 2)

    # Derived ratios per channel, computed AFTER summing so they are never averaged.
    for channels in out.values():
        for bucket in channels.values():
            bucket["net_pct"] = _ratio(bucket["net"], bucket["sales"])
            bucket["acos"] = (
                _ratio(bucket["ads_cost"], bucket["ad_attributed_sales"])
                if bucket["ads_cost"] else None
            )
            bucket["acos_infinite"] = bool(
                bucket["ads_cost"] and not bucket["ad_attributed_sales"]
            )
    return out


def portfolio(
    econ_rows: Sequence,
    catalogue: Mapping,
    ratings: Mapping,
    decisions: Mapping | None = None,
    today: date | None = None,
    *,
    ads_by_asin: Mapping | None = None,
    channels: Mapping | None = None,
    thresholds: Mapping | None = None,
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

    ``ads_by_asin`` carries the Advertising API figures, ``channels`` the merchant/FBA split, and
    ``thresholds`` the owner's edited rules. All optional: without ads credentials the tab shows
    margins and TACOS exactly as it did before ACOS existed.
    """
    decisions = decisions or {}
    limits = thresholds_or_default(thresholds)
    channels = channels or {}
    by_parent: dict[str, dict] = {}
    unmatched: set[str] = set()

    for econ in econ_rows:
        size = size_row(econ, catalogue, ads_by_asin)
        if not size["asin"]:
            continue
        if not size["known"]:
            unmatched.add(size["asin"])
        size["channels"] = channels.get(size["asin"]) or {}

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

        verdict, reason = verdict_for(agg, rating=rating, sizes=sizes, thresholds=limits)
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

    # ── The SKU view: the same sizes, flattened, each carrying its parent's name ──
    #
    # **A relabelling, not a second fetch.** The rows are the identical `size` dicts already
    # under each parent, so a SKU row and its parent can never disagree about a number. Each
    # already has merchant and FBA combined, because the CHILD_ASIN aggregation Amazon performs
    # sums both — which is what "the easy ship and fba sku's combined data" asks for.
    #
    # A size carries its OWN verdict here, judged on economics alone. Ratings are deliberately
    # excluded: Amazon pools reviews per variation family, so every size of one product reports
    # the same stars, and letting that decide a per-size verdict would imply a precision the
    # review data does not have.
    sku_rows = []
    for parent in parents:
        for size in parent["sizes"]:
            verdict, reason = verdict_for(size, rating=None, sizes=(), thresholds=limits)
            sku_rows.append({
                **size,
                "product": size.get("product") or parent["product"],
                "brand": size.get("brand") or parent["brand"],
                "parent_product": parent["product"],
                "parent_verdict": parent["verdict"],
                "rating": parent["rating"],
                "rating_count": parent["rating_count"],
                "verdict": verdict,
                "verdict_reason": reason,
                "decision": parent["decision"],
            })
    sku_rows.sort(key=lambda row: (-row["sales"], row["asin"]))
    sku_counts = {verdict: 0 for verdict in VERDICT_ORDER}
    for row in sku_rows:
        sku_counts[row["verdict"]] = sku_counts.get(row["verdict"], 0) + 1

    return {
        "parents": parents,
        "skus": sku_rows,
        "totals": {
            **totals,
            "parents": len(parents),
            "skus": len(sku_rows),
            "verdicts": counts,
            "sku_verdicts": sku_counts,
        },
        "thresholds": limits,
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
    # The Advertising API figures, summed the same way. `ads_cost` is deliberately kept apart
    # from `ad_spend`: they come from two APIs with different attribution windows (they
    # reconcile to 0.2% account-wide) and dividing one by the other's denominator would be a
    # ratio of two different things.
    ads_cost = round(sum(_num(s.get("ads_cost")) for s in sizes), 2)
    attributed = round(sum(_num(s.get("ad_attributed_sales")) for s in sizes), 2)
    ad_clicks = sum(int(s.get("ad_clicks") or 0) for s in sizes)
    ad_impressions = sum(int(s.get("ad_impressions") or 0) for s in sizes)

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
        "ads_cost": ads_cost,
        "ad_attributed_sales": attributed,
        "ad_clicks": ad_clicks,
        "ad_impressions": ad_impressions,
        "net_pct": _ratio(net, sales),
        # TWO ad ratios, and they answer different questions:
        #   tacos = spend / TOTAL sales      -> how ad-dependent is this product?
        #   acos  = spend / ATTRIBUTED sales -> do the ads pay for themselves?
        # Measured account-wide: TACOS 33.1% against ACOS 89.9%. Collapsing them into one number
        # would lose a real distinction, which is why both are on screen.
        "tacos": _ratio(ads, sales),
        "acos": _ratio(ads_cost, attributed) if ads_cost else None,
        "acos_infinite": bool(ads_cost and not attributed),
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
