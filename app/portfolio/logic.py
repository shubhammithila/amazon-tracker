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

import re
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

# **There were SEVEN, and the one removed was the only rule that read ACOS.** Taken out on the
# owner's instruction: *"TACOS play a role, ACOS doesnt in deciding the kill scale maintain. as
# ACOS might not be perfectly captured by online tools. but TACOS is."*
#
# ACOS is still computed, still a sortable and filterable COLUMN, and still named in the BEST BET
# and MONITOR reasons. The rule is that no verdict may **branch** on it — `verdict_for` reads
# `acos` only to print it. `test_no_verdict_rule_reads_acos` is the guard, and it varies the ads
# figures rather than asserting a retired name is absent, so reintroducing an ACOS branch under
# some other name fails too.
#
# **The cost was measured before agreeing to it**, on the live 30-day window: 5 of 90 parents
# move, and two move from a warning to an endorsement — ABC Sattu becomes BEST BET at 106% ACOS,
# Makhana Powder becomes SCALE at 401%. That is an accepted trade rather than an oversight, and
# what makes it acceptable is that ACOS stays on screen beside them, red above break-even, so the
# number is visible even though nothing decides on it.
#
# Historical `product_decision.snapshot_json` rows may still carry the retired string. Nothing
# looks a snapshot verdict up, and both verdict-keyed maps below are total-with-fallback, so a
# legacy value degrades to "Maintain, no flag" rather than raising.

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


# ─── Three groups over the seven verdicts ─────────────────────────────────────
#
# Asked for as *"lesser tabs — I want only Scale (top performers), Maintain (mid performers),
# Kill or monitor (least performers)"*.
#
# **A VIEW over `verdict_for`, not a replacement for it.** The seven rules stay exactly as they
# are and so does every reason string, because each reason is a claim the owner can check against
# Seller Central and overrule — and CLAUDE.md records what each one cost to learn:
#
#   * rule 1 must stay FIRST: a product that sold 2 units reported **+505.6% net** (a refund
#     reversal landed in the window). On margin alone it is the best product in the portfolio.
#   * rule 3 is AND, not OR: a negative margin at low TACOS is a pricing problem worth fixing,
#     while one sustained by heavy spend is a product being bought only because it is paid for.
#   * rule 4 is why this tab expands to sizes at all.
#
# Rewriting seven rules into three would discard all of that and leave a verdict nobody can
# verify. Mapping keeps every rule and every reason; only the tab count changes.

GROUP_SCALE = "Scale"
GROUP_MAINTAIN = "Maintain"
GROUP_KILL = "Kill or monitor"

#: Best first, unlike `VERDICT_ORDER` which is worst-first. The three-tab strip answers "where is
#: the growth, what is steady, what needs a decision"; the old worst-first order was for a
#: seven-chip WORKLIST, which is a different question.
GROUP_ORDER = (GROUP_SCALE, GROUP_MAINTAIN, GROUP_KILL)

#: Which group each verdict belongs to. **Total over `VERDICT_ORDER`**, asserted by a test, so a
#: new verdict cannot be added without deciding where it goes — otherwise a product silently
#: vanishes from all three tabs.
VERDICT_GROUPS: dict[str, str] = {
    VERDICT_BEST_BET: GROUP_SCALE,
    VERDICT_SCALE: GROUP_SCALE,
    VERDICT_MONITOR: GROUP_MAINTAIN,
    # SURGICAL is Maintain, not Kill: the parent EARNS its place and one size does not, so the
    # action is surgery rather than a kill. It carries a flag naming the losing size, because in
    # Maintain without one it reads as simply fine — measured live, Cheese & Cream Roasted Chana
    # earns +27.1% overall while one 250 g pack burns 103% TACOS at -52.7% net.
    VERDICT_SURGICAL: GROUP_MAINTAIN,
    VERDICT_KILL: GROUP_KILL,
    VERDICT_DEAD: GROUP_KILL,
}


def verdict_group(verdict: str) -> str:
    """Which of the three tabs a verdict belongs to.

    Falls back to ``Maintain`` for an unrecognised verdict rather than dropping the row. A product
    missing from all three tabs is invisible; one in the middle tab is merely mis-sorted, and its
    row still shows its own verdict and reason. Same deny-into-the-safe-bucket reasoning as
    `ads.logic.manager_of` treating an unknown campaign name as ours.
    """
    return VERDICT_GROUPS.get(verdict, GROUP_MAINTAIN)


#: The verdicts whose real action is NOT what their group implies, so the row must say so.
#:
#: **One entry, and it used to be two.** The other was AD DEPENDENT, retired with its rule — see
#: the note beside `VERDICT_ORDER`. Deliberately still a DICT rather than collapsed into a single
#: `if verdict == VERDICT_SURGICAL` check: the mechanism is what makes adding the next awkward
#: verdict a one-line change, and the `dict` is what `test_exactly_ONE_verdict_carries_a_group_flag`
#: asserts in both directions so a second cannot appear unnoticed.
GROUP_FLAGS: dict[str, str] = {
    VERDICT_SURGICAL: "some sizes lose money",
}


def group_flag(verdict: str) -> str:
    """The warning a row needs because its group hides its real action, or ``""``.

    SURGICAL does not fold cleanly into three groups, and folding it SILENTLY is the actual risk:
    in Maintain it reads as a healthy product, when in fact the parent earns its place while one
    pack size does not. The flag is how that survives the simplification.
    """
    return GROUP_FLAGS.get(verdict, "")


def group_counts(rows: Sequence[Mapping]) -> dict[str, int]:
    """How many rows land in each of the three groups.

    Every group is present even at zero, so a tab that matches nothing renders as an empty tab
    rather than vanishing — the Portfolio tab already learned this with the verdict chips, where
    dropping a zero-count chip left an empty table, nothing highlighted, and no control left to
    click to undo the filter.
    """
    counts = {group: 0 for group in GROUP_ORDER}
    for row in rows:
        group = verdict_group(str(row.get("verdict") or ""))
        counts[group] = counts.get(group, 0) + 1
    return counts


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

# **`BREAK_EVEN_ACOS = 1.00` was here**, and it went with the AD DEPENDENT rule it was the only
# threshold for. Not replaced by anything: it was break-even rather than a tuning choice, so there
# is no equivalent number to make editable on the TACOS side.
#
# The measurement it recorded is still true and still worth knowing — account-wide TACOS 33.1%
# against a true ACOS of **89.9%**, so Rs 1 of ads returns Rs 1.11 of attributed sales, and
# individual products run far worse (B0GW388QP6 spends Rs 36,514 to earn Rs 12,815, 285%). It is
# now context a human reads off the ACOS column rather than a line the code draws.
#
# A settings row stored before the removal may still hold a `break_even_acos` key. Both
# `thresholds_or_default` and `save_settings` gate on `key in DEFAULT_THRESHOLDS`, so it is inert
# rather than harmful, and rewriting stored JSON to tidy it away would be a migration for no gain.

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


#: The range each threshold may legally take, as ``(low, high)`` inclusive.
#:
#: **These are not preferences, they are the bounds outside which the RULE stops meaning
#: anything.** Found by /qa: the settings route accepted `good_rating: 99` and `kill_tacos: -1`.
#: The first zeroed BEST BET (no product can be rated 99 stars) and the second made the help text
#: read "TACOS above -100%", i.e. every loss-making product is a KILL regardless of ad spend. Both
#: were stored, both silently changed what a verdict meant, and the screen presented the nonsense
#: as a rule.
#:
#: Ratings are bounded by Amazon's own scale (1-5). Ratios are bounded at 0 below — a negative
#: margin or TACOS threshold is not a stricter rule, it is a broken one — and generously above,
#: because a 200% TACOS ceiling is a legitimate thing to want on an account where products run
#: that high.
THRESHOLD_RANGES = {
    "dead_units": (0, 1000),
    "returns_kill_rate": (0.0, 1.0),        # a return rate cannot exceed 100%
    "returns_min_units": (1, 10000),        # 0 would make the rule fire on no evidence
    "good_net": (0.0, 5.0),
    "good_tacos": (0.0, 5.0),
    "kill_tacos": (0.0, 5.0),
    "good_rating": (1.0, 5.0),              # Amazon's star scale, so 99 is not a stricter bar
}


#: Which DECISION each threshold serves, so the settings panel can be grouped the way the owner
#: asked for it: *"give me editabel metrics for what to put in kill, maintain and scale."*
#:
#: **Total over `DEFAULT_THRESHOLDS`, asserted by a test** — the same discipline `VERDICT_GROUPS`
#: carries over `VERDICT_ORDER`. A new threshold then cannot be added without deciding which
#: decision it serves; the alternative is a silent fall-through into whichever heading happens to
#: render last, which is how a rule ends up filed under the wrong question.
#:
#: **`Maintain` is deliberately absent, and the panel says so in words rather than rendering an
#: empty heading.** MONITOR is the last rule — "everything else" — so it genuinely has no number
#: of its own. That is a fact about the rules, not a gap in this dict: a heading with no inputs
#: reads as broken, while a heading with one sentence explaining why reads as the design. Same
#: reasoning as every group tab rendering at zero, dashed, rather than disappearing.
#:
#: Shipped to the screen rather than hardcoded there, like `verdict_groups` and `phase_labels`,
#: because a second copy of the grouping is a second thing to keep in step.
THRESHOLD_GROUPS: dict[str, str] = {
    # Scale: what makes a product worth spending MORE on.
    "good_net": GROUP_SCALE,
    "good_tacos": GROUP_SCALE,
    "good_rating": GROUP_SCALE,
    # Kill: what makes a product worth stopping.
    "kill_tacos": GROUP_KILL,
    "returns_kill_rate": GROUP_KILL,
    "returns_min_units": GROUP_KILL,
    "dead_units": GROUP_KILL,
}


def threshold_error(key: str, value) -> str | None:
    """``None`` if ``value`` is a legal setting for ``key``, else a message fit for the screen.

    Used by the route to refuse rather than store. A threshold that silently accepts nonsense is
    worse than one that cannot be edited: the owner believes a rule moved, sees verdicts change,
    and has no way to know the number was never meaningful.
    """
    if key not in DEFAULT_THRESHOLDS:
        return f"Unknown threshold {key!r}."
    try:
        number = float(value)
    except (TypeError, ValueError):
        return f"{key} must be a number, got {value!r}."
    if number != number or number in (float("inf"), float("-inf")):   # NaN or infinity
        return f"{key} must be a real number."
    low, high = THRESHOLD_RANGES[key]
    if not (low <= number <= high):
        if key == "good_rating":
            return (f"{key} must be between {low:g} and {high:g} — Amazon rates products out of "
                    f"5 stars, so {number:g} could never be reached.")
        if key in ("dead_units", "returns_min_units"):
            return f"{key} must be between {low:g} and {high:g} units, got {number:g}."
        return (f"{key} must be between {low:.0%} and {high:.0%} (as a ratio, "
                f"{low:g} to {high:g}), got {number:g}.")
    return None


def thresholds_or_default(thresholds: Mapping | None) -> dict:
    """A complete threshold dict, filling anything absent or unusable from the measured defaults.

    Callers may pass a partial set (the settings row only stores what was edited), and a missing
    key must fall back rather than raise — a half-saved settings row would otherwise take the
    whole dashboard down.

    **Out-of-range values fall back too, not just unparseable ones.** The route refuses them at the
    boundary, but a row written before that guard existed (or by hand) must not be able to make a
    verdict meaningless — so the same bounds apply on the way out.
    """
    merged = dict(DEFAULT_THRESHOLDS)
    for key, value in (thresholds or {}).items():
        if key in DEFAULT_THRESHOLDS and value is not None:
            if threshold_error(key, value) is None:
                merged[key] = float(value)
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


#: Merchant SKUs on this account mark FBA with a trailing "FBA" token: `0.25 fc np` is the
#: merchant/Easy Ship listing, `0.25 fc np FBA` the Amazon-fulfilled one. The suffix is the only
#: marker Amazon gives at this grain.
#:
#: **This is a convention of THIS account, not an Amazon rule** — and it is TWO conventions, which
#: is what the first version of this note missed. It said the suffix was "verified across all 453
#: MSKU economics rows and all 213 advertised SKUs", which was true and still concluded the wrong
#: thing: the brands separate the token differently.
#:
#:     Mithila Foods   0.5kg cs 1 FBA          space-separated
#:     Howrah Foods    HF_CBchana_0.25kg_FBA   UNDERSCORE-separated
#:     Prayagraj       PR_BP_0.2_FBA           underscore-separated
#:
#: Splitting on whitespace alone therefore read the whole Howrah string as one token and classified
#: every HF and PR listing as merchant. Measured on the 22 Sep inventory file: **27 SKUs misfiled,
#: hiding 798 units of real FBA stock** from `parse_stock_csv` — so `deficit = projection - fba_stock`
#: told the owner to manufacture stock he already held. On the Portfolio tab the same fault put
#: **41 of 558 SKUs** and Rs 80,839 of sales in the wrong channel bucket.
FBA_SKU_SUFFIX = "FBA"

#: Whitespace, underscore and hyphen all separate tokens in a merchant SKU. Widening the separator
#: set is NOT the same as widening to a substring test — see `_channel_of`.
_SKU_SEPARATORS = re.compile(r"[\s_\-]+")

CHANNEL_FBA = "fba"
CHANNEL_MERCHANT = "merchant"


def _channel_of(seller_sku) -> str:
    """Which fulfilment channel a merchant SKU belongs to. See `FBA_SKU_SUFFIX`.

    **A TOKEN test, never a substring test**, and that distinction is the whole reason this function
    exists rather than an inline ``"FBA" in sku``: a product whose name happens to contain "fba" —
    ``fbagel 1kg`` — must stay merchant. The same care `shipment.logic.is_easy_ship` takes with its
    "EZ" token, for the same reason.

    The separator set covers space, underscore and hyphen because this account uses two naming
    conventions (Mithila space-separated, Howrah/Prayagraj underscore-separated). That widens which
    characters END a token; it does not weaken the test to a substring match, so ``fbagel 1kg`` is
    still merchant and a test asserts it.
    """
    parts = _SKU_SEPARATORS.split(str(seller_sku or "").upper().strip())
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
    # **`acos` is read to PRINT, never to compare.** No rule below may branch on it — see the note
    # where the fifth rule used to be. It stays in the reason because "net 31.5% at 29.5% TACOS,
    # ACOS 106%" is a claim the owner can check against Seller Central, and a verdict he can
    # overrule, which is the whole reason these reasons carry numbers.
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
        # **Name the FLAVOUR too when the parent has more than one**, or the reason reads
        # "2 size(s) lose money (250 g, 250 g)" — which names two different products
        # identically and gives the owner nothing to act on. Decided from `sizes` rather than
        # passed in, so the reason cannot disagree with the rows it is describing.
        multi = len({str(s.get("product") or "") for s in sizes if s.get("product")}) > 1
        names = ", ".join(_size_label(s, with_flavour=multi) for s in losers[:3])
        return VERDICT_SURGICAL, (
            f"the product earns {net_pct * 100:.1f}% overall, but {len(losers)} size(s) "
            f"lose money ({names}) — kill those, keep the rest"
        )

    # **There was a FIFTH rule here — AD DEPENDENT — and it was the only rule in this function
    # that branched on ACOS.** Removed on the owner's instruction, because he does not trust ACOS
    # to be captured correctly while he does trust TACOS. See the note beside `VERDICT_ORDER` for
    # the measured cost.
    #
    # `acos` is still read below, twice, to PRINT it. That is the whole distinction: the number
    # informs the owner, it does not decide. Nothing between here and the end of the function may
    # compare it against anything — `test_no_verdict_rule_reads_acos` varies the ads figures
    # across four shapes and asserts the verdict does not move, so an ACOS branch reintroduced
    # under any name fails.

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


def _size_label(size: Mapping, *, with_flavour: bool = False) -> str:
    """A pack size named for a human: "500 g" if the weight is known, else the ASIN.

    ``with_flavour`` prefixes the child's own catalogue name, for the parents that vary by
    flavour as well as by weight — see `flavour_groups`. Off by default because for 85 of the 90
    parents the flavour IS the parent name, and repeating it on every size row is noise.
    """
    from app.shipment.logic import weight_label

    weight = float(size.get("weight") or 0)
    label = weight_label(weight) if weight else (size.get("asin") or "?")
    flavour = str(size.get("product") or "").strip()
    return f"{flavour} {label}" if with_flavour and flavour else label


# ─── Two variation dimensions, not one ───────────────────────────────────────
#
# **A parent can vary by FLAVOUR as well as by weight, and the tab only knew about weight.**
# Measured on the live account: 5 of the 90 parents hold more than one flavour, and the worst
# case is a single parent holding **15 sizes = 5 flavours x 3 weights**, every row labelled by
# weight alone — so the expanded product read as "500 g, 500 g, 250 g, 250 g, 250 g, 250 g,
# 1 kg, 1 kg, 500 g, ..." with no way to tell which flavour any row belonged to. Three rows
# genuinely said "250 g" and were three different products.
#
# The flavour is already on every size row: `size["product"]` is the catalogue NAME of the child
# ASIN, which differs per flavour while the parent carries only the first one Amazon happened to
# list. That is also why the parent was called "Cheese & Cream Roasted Chana" when Cheese & Cream
# is 3 of its 15 sizes and its SMALLEST seller — the name was an accident of iteration order.


def _tokens(name: str) -> list[str]:
    return [t for t in str(name or "").split() if t]


def _shared_run(token_lists: Sequence[Sequence[str]], *, from_end: bool) -> list[str]:
    """The longest run of tokens every name shares, read from one end.

    Case-insensitive, because the catalogue mixes "urad dal badi" with "Chana dal badi" and a
    case-sensitive compare would find nothing in common between them. The casing returned is the
    first list's, which callers pass as the biggest seller's — so the label reads the way the
    best-known product spells it.
    """
    if not token_lists:
        return []
    shortest = min(len(t) for t in token_lists)
    shared = 0
    while shared < shortest:
        index = -(shared + 1) if from_end else shared
        word = token_lists[0][index].casefold()
        if any(other[index].casefold() != word for other in token_lists[1:]):
            break
        shared += 1
    if not shared:
        return []
    return list(token_lists[0][-shared:] if from_end else token_lists[0][:shared])


def family_label(names: Sequence[str]) -> str:
    """What a set of flavour names have in common, as a name for their shared product.

    Asked for as *"the parent name can be flavours of chana"* — so the label is derived rather
    than typed, because five parents need it and a hand-written name for each would go stale the
    moment a flavour is added.

    **Both ends are checked, and they are joined when both exist.** Measured against all five
    multi-flavour parents on the account, which need three different shapes:

    ======================================  ============================
    the flavours                            the shared name
    ======================================  ============================
    Nimbu Pudina / Peri Peri / ... Chana    ``Roasted Chana`` (end only)
    Desi Tilkut / Desi Tilkut - Jaggery     ``Desi Tilkut`` (start only)
    Bengali Moong / Urad **dal bori**       ``Bengali dal bori`` (both)
    ======================================  ============================

    Taking one end only would have called the last of those "dal bori", dropping the brand line
    the products are actually sold under. The runs cannot be allowed to overlap: two names
    differing in one middle token would otherwise emit that token twice.

    Falls back to the first name — passed as the biggest seller's — when nothing is shared, which
    is honest: an unrelated set has no family name, and inventing one would be worse than
    repeating a member's.
    """
    clean = [_tokens(name) for name in names if str(name or "").strip()]
    if not clean:
        return ""
    if len(clean) == 1:
        return " ".join(clean[0])

    head = _shared_run(clean, from_end=False)
    tail = _shared_run(clean, from_end=True)
    # Overlap guard: with names like "A x B" / "A y B" the head is [A] and the tail is [B] and
    # they are disjoint, but "A B" / "A B C" would give head [A, B] and tail [B] — emitting B
    # twice. The shortest name bounds how many tokens can legitimately be claimed in total.
    shortest = min(len(t) for t in clean)
    if len(head) + len(tail) > shortest:
        return " ".join(tail or head)
    shared = head + tail
    return " ".join(shared) if shared else " ".join(clean[0])


def flavour_groups(sizes: Sequence[Mapping]) -> list[dict]:
    """``sizes`` regrouped by flavour, each group's weights beneath it, biggest seller first.

    ``[]`` when the parent has only one flavour — 85 of the 90 parents — so the screen keeps
    rendering a flat list of weights for them rather than growing a pointless heading level.
    The caller checks for emptiness rather than for a count, so "does this need grouping" is
    asked in exactly one place.

    Each group carries the summed economics of its own sizes, computed by ``_sum_sizes`` like
    every other total here, so a flavour heading and the weights under it can never disagree.
    """
    by_flavour: dict[str, list] = {}
    for size in sizes:
        by_flavour.setdefault(str(size.get("product") or "").strip(), []).append(size)
    if len(by_flavour) < 2:
        return []

    groups = []
    for flavour, rows in by_flavour.items():
        # Heaviest revenue first WITHIN the flavour, matching how sizes are ordered elsewhere.
        rows = sorted(rows, key=lambda s: (-_num(s.get("sales")), s.get("asin") or ""))
        groups.append({
            "flavour": flavour or (rows[0].get("asin") or ""),
            "sizes": rows,
            **_sum_sizes(rows),
        })
    groups.sort(key=lambda g: (-g["sales"], g["flavour"].casefold()))
    return groups


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


# ─── Category sales ───────────────────────────────────────────────────────────
#
# Asked for as *"need each category sales as well. Sattu, chana, flours, Staples, seeds, others.
# Or you can check the category/priority thing from the shipment tab."*
#
# **The labels and the keyword rules are IMPORTED from `app.shipment.logic`, never copied.** That
# module's docstring is explicit that the rule ORDER is the rule, not an implementation detail:
# nine of the 74 real product names match several keywords and three change bucket depending on
# which is tested first — "Bangla Chana Sattu" is a sattu, "Rice Atta" is a flour, "chana dal badi"
# is a chana. A second copy here would be a second thing to keep in step, and the failure would be
# a category total that disagrees with the packer's sort order.

#: Products with no stored category get their own bucket rather than falling into "Rest".
#:
#: Measured on production: only **38 of ~90** Portfolio parents have a row in
#: `product_categories`. Folding the rest into Rest would make Rest the largest category and stop
#: it meaning anything, and keyword-guessing them silently would hide a wrong guess. They are
#: counted here and NAMED on screen, the way the Shipment catalogue notes and the Projections
#: `needs_review` list already do, so they can be classified once on the Shipment tab.
CATEGORY_UNCLASSIFIED = "Unclassified"

#: How many unclassified product names to name on screen. A list can be 52 long; the screen needs
#: a sentence, not a column — the same cap `MISSING_DAYS_SHOWN` and the catalogue notes use.
UNCLASSIFIED_SHOWN = 8


def _first_category(names: Sequence[str], categories: Mapping[str, int]) -> int | None:
    """The stored priority for the first of ``names`` that has one, or ``None``.

    Tried in order, parent name first, because a multi-flavour parent's own name is DERIVED and may
    not be a catalogue name at all — while its sizes' names are.

    **Exact match only, deliberately.** `shipment.logic.category_for` does substring keyword
    matching and is the right tool for GUESSING a category; this function reads what the owner
    actually CHOSE. Falling back to a keyword guess here would make a wrong guess indistinguishable
    from a decision, which is exactly what naming the unclassified products is meant to avoid.
    """
    for name in names:
        key = str(name or "").casefold().strip()
        if key and key in categories:
            return categories[key]
    return None


def category_totals(
    parents: Sequence[Mapping],
    categories: Mapping[str, int] | None = None,
) -> dict:
    """Sales, ad spend and net per category, from the parent rows.

    ``categories`` maps a **casefolded product name** to a 1..6 priority, exactly as
    `product_categories` stores it — so this function stays pure and the SQL lives in the
    repository.

    Built from the PARENT rows rather than from the raw economics, so a category total is the sum
    of the rows on screen. A separate aggregation over `econ_rows` would be a second number for
    one thing, which is the defect this codebase records three times (the Orders tab's "86 orders
    beside 87 lines"; the Portfolio parent rows that exist to prevent it).

    **Every percentage is recomputed from the summed numerator and denominator, never averaged.**
    The mean of twenty products' TACOS weights a product that sold 1 unit equally with one that
    sold 400 and produces a number belonging to no product — the same rule `_sum_sizes` follows.
    """
    from app.shipment.logic import CATEGORY_LABELS

    categories = categories or {}
    buckets: dict[str, dict] = {}
    unclassified_names: list[str] = []

    for parent in parents:
        name = str(parent.get("product") or parent.get("parent_product") or "").strip()
        # **The SIZE names, not just the parent's.** The parent name is DERIVED: `family_label`
        # renames a multi-flavour parent to what its flavours share, and disambiguates a collision
        # by appending a count — so the row reads "Roasted Chana (5 flavours)" while
        # `product_categories` holds "roasted chana", "peri peri roasted chana" and so on.
        #
        # Found on real production data: matching the parent name alone left 52 of 90 products
        # unclassified, including several whose categories ARE stored. The size names are the real
        # catalogue names, which is what that table is keyed on.
        candidates = [name] + [
            str(size.get("product") or "") for size in parent.get("sizes") or []
        ]
        priority = _first_category(candidates, categories)
        if priority is None:
            label = CATEGORY_UNCLASSIFIED
            if name and name not in unclassified_names:
                unclassified_names.append(name)
        else:
            label = CATEGORY_LABELS.get(priority, CATEGORY_LABELS[6])

        bucket = buckets.setdefault(
            label,
            {
                "category": label,
                "products": 0,
                "sales": 0.0,
                "ad_spend": 0.0,
                "net": 0.0,
                "units": 0,
                # Which parents landed here, so the SCREEN can filter its table by category
                # without re-deriving the classification. Re-deriving would be a second rule, and
                # it would disagree on exactly the rows hardest to notice — the multi-flavour
                # parents whose displayed name is not a catalogue name at all.
                "products_named": [],
            },
        )
        bucket["products"] += 1
        if name:
            bucket["products_named"].append(name)
        bucket["sales"] += _num(parent.get("sales"))
        bucket["ad_spend"] += _num(parent.get("ad_spend"))
        bucket["net"] += _num(parent.get("net"))
        bucket["units"] += int(parent.get("units") or 0)

    for bucket in buckets.values():
        # Recomputed from the sums, never averaged. `_ratio` returns None with no denominator,
        # because a category with no sales has no TACOS and 0% would rank it as the most
        # ad-efficient thing in the portfolio.
        bucket["tacos"] = _ratio(bucket["ad_spend"], bucket["sales"])
        bucket["margin"] = _ratio(bucket["net"], bucket["sales"])

    # Biggest first: the strip is read as "where is the money", not as a fixed taxonomy. Ties
    # broken by name so the order is stable between renders.
    ordered = sorted(
        buckets.values(), key=lambda b: (-b["sales"], b["category"])
    )
    # **Two different counts, and the screen has to say which is which.** Found on live production:
    # the Unclassified CARD read 55 while the note read "51 product(s)" — 55 unclassified ROWS over
    # 51 distinct NAMES, because `Singhara Atta` appears 4 times in the catalogue and
    # `Arwa Katarni Rice` twice. Both figures are right and the pair reads as a bug, which is the
    # "86 orders beside 87 lines" defect this codebase records three times.
    #
    # The names count is the one that matters for the ACTION — a category is stored per name, so
    # classifying "Singhara Atta" once fixes all four rows — and `unclassified_rows` travels so the
    # note can say so rather than leaving the reader to spot the difference.
    unclassified_rows = sum(
        bucket["products"]
        for label, bucket in buckets.items()
        if label == CATEGORY_UNCLASSIFIED
    )
    return {
        "categories": ordered,
        "unclassified_names": unclassified_names[:UNCLASSIFIED_SHOWN],
        "unclassified_total": len(unclassified_names),
        "unclassified_rows": unclassified_rows,
    }


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
        # The brand is consistent across a family, so the first non-empty one is the row's.
        # **The NAME is not taken this way any more**, and that was a real defect: it took
        # whichever flavour Amazon happened to list first, so a parent holding 5 flavours was
        # named after the one that sold LEAST. It is derived from all of them below instead.
        if not parent["brand"] and size["brand"]:
            parent["brand"] = size["brand"]

    parents = []
    for parent_asin, parent in by_parent.items():
        sizes = sorted(parent["sizes"], key=lambda s: (-s["sales"], s["asin"]))
        agg = _sum_sizes(sizes)
        rating_row = _rating_for(sizes, ratings)
        rating = rating_row.get("rating")

        # The flavour dimension. `groups` is empty for the 85 single-flavour parents, so the
        # screen only grows a heading level where there is genuinely a second dimension.
        groups = flavour_groups(sizes)
        # Names are passed BIGGEST SELLER FIRST (`sizes` is already sorted that way), because
        # `family_label` returns the leading name's casing when nothing is shared and its
        # spelling of the shared tokens when something is.
        flavour_names = list(dict.fromkeys(s["product"] for s in sizes if s["product"]))
        product = (
            family_label(flavour_names) if len(groups) > 1
            else (flavour_names[0] if flavour_names else "")
        )

        verdict, reason = verdict_for(agg, rating=rating, sizes=sizes, thresholds=limits)
        decision = decisions.get(parent_asin) or {}
        parents.append({
            "parent_asin": parent_asin,
            "product": product or parent_asin,
            "brand": parent["brand"],
            # The flavour dimension, empty when the parent has only one.
            "flavour_groups": groups,
            "flavours": [g["flavour"] for g in groups],
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

    # **A derived name can COLLIDE with a real one, and on this account it does.**
    #
    # Measured after the rename: B0DWFC3QT9 holds 5 flavours whose shared name is "Roasted Chana",
    # and B0CY8HFJT9 is a *different* parent already CALLED "Roasted Chana" — 1,309 units against
    # 1,301, so the two sat next to each other in the table as apparent duplicates with no way to
    # tell which was which. Both are legitimate products; the ambiguity is entirely an artefact of
    # shortening one of them.
    #
    # The disambiguation only ever touches the DERIVED name, never a catalogue one: the product
    # actually called "Roasted Chana" keeps its name, and the family that was shortened into a
    # collision says what it is a family OF. A suffix rather than a prefix so the shared words
    # still lead, which is what makes the rows sort and scan together.
    #
    # (`Singhara Atta` appears four times and `Govindbhog Rice` twice, both from the catalogue
    # itself and neither introduced here — real separate listings with the same name, left alone
    # because renaming what Amazon and the sheet agree on would be this function overreaching.)
    derived = {p["parent_asin"] for p in parents if p["flavours"]}
    taken: dict[str, int] = {}
    for parent in parents:
        taken[parent["product"]] = taken.get(parent["product"], 0) + 1
    for parent in parents:
        if parent["parent_asin"] in derived and taken.get(parent["product"], 0) > 1:
            parent["product"] = f"{parent['product']} ({len(parent['flavours'])} flavours)"

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
