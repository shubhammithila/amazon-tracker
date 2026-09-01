"""Pure rules for the Ads tab. No database, no network.

**This is the first module in this app whose output CHANGES the seller account.** Everything
else here reads Amazon and writes only our own records; a rule run computed in this file becomes
a `PUT` that moves real bids and therefore real money. Every guard below exists because of
something measured against the live account on 2026-08-28, and the module is pure so that all of
it is testable without touching Amazon.

Three facts drive the design, all measured:

1. **One rule writes to TWO endpoints, and the report hides which.** The `spTargeting` report
   labels both id columns `keywordId`, but only `EXACT`/`PHRASE`/`BROAD` rows are keywords. The
   `TARGETING_EXPRESSION*` rows are targeting clauses on a different endpoint. Of six real matches
   from the owner's own rule, four were targeting clauses. Sending one to `/sp/keywords` returns a
   `207` with an error array — a silent partial failure inside a run that looks successful. See
   `writer_for`.

2. **Amazon enforces a bid FLOOR but effectively no CEILING.** Measured: `bid=0.5` was rejected
   (`rangeError`, "lower than the minimum allowed by the marketplace"); **`bid=1000.0` was
   ACCEPTED** on an account whose median enabled bid is ₹6.39. So a mistyped multiplier is caught
   here or not at all. See `GUARDRAILS` and `check_guardrails`.

3. **ROAS has no meaning without spend.** A zero-spend row must not match `roas < 3`, or every
   dormant target in the account is swept into a bid cut. `_ratio` returns `None`, exactly as the
   Portfolio tab's does, and `None` never satisfies a comparison.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta

from app import ist

#: India is UTC+5:30, and the ledger stores naive `datetime.utcnow()`.
#:
#: **Re-exported from `app.ist`, not defined here.** This module used to own the offset, under a
#: comment noting FOUR bugs from this boundary. There are now SIX, and the sixth was the nightly
#: scheduler firing at 09:20 IST — a file this offset could not reach. An offset that lives in
#: whichever module happened to need it first is how a codebase ends up with three of them, so it
#: moved up to `app/ist.py` and the name stays here because four call sites use it.
IST_OFFSET = ist.IST_OFFSET


def ist_day(when) -> str:
    """The IST calendar date of a naive UTC datetime, as ``YYYY-MM-DD``. ``""`` for None.

    "Not twice on the same day" is a decision taken in IST, and the ledger records UTC. A change
    applied at 04:00 IST is 22:30 UTC the PREVIOUS day, so a UTC-day comparison would call it
    yesterday and allow a second run that morning — 5.5 hours out of every 24 in which the guard
    silently would not hold.

    Delegates to `ist.day_of`; the name and this docstring stay because they are what the bid guard's
    four call sites read, and because the reasoning above is about the guard rather than about
    timezones in general.
    """
    return ist.day_of(when)

# ─── Routing: which endpoint owns this row ───────────────────────────────────
#
# **The report calls both id columns `keywordId`, which is the trap.** Routing is decided by
# `matchType`, whose real vocabulary was read off a 12,854-row report rather than the docs.

#: Keyword match types. These rows are written with `{keywordId, bid}` to `/sp/keywords`.
#: Which Amazon ad product a row belongs to. A first-class dimension rather than a boolean, because
#: Sponsored Display is a plausible third and adding it should be a fetch plus a writer rather than
#: a redesign.
AD_PRODUCT_SP = "sp"
AD_PRODUCT_SB = "sb"

KEYWORD_MATCH_TYPES = frozenset({"EXACT", "PHRASE", "BROAD"})

#: Targeting-clause match types, written with `{targetId, bid}` to `/sp/targets`:
#:
#: * `TARGETING_EXPRESSION_PREDEFINED` — Amazon's auto targets. Measured on this account:
#:   `QUERY_HIGH_REL_MATCHES` (close-match), `QUERY_BROAD_REL_MATCHES` (loose-match),
#:   `ASIN_ACCESSORY_RELATED` (complements), `ASIN_SUBSTITUTE_RELATED` (substitutes).
#: * `TARGETING_EXPRESSION` — manual product and category targets, e.g. `category="4860253031"`.
TARGET_MATCH_TYPES = frozenset({"TARGETING_EXPRESSION_PREDEFINED", "TARGETING_EXPRESSION"})

WRITER_KEYWORD = "keyword"
WRITER_TARGET = "target"
#: Sponsored Brands keywords. A THIRD writer, not a variant of the first: the endpoint differs
#: (`/sb/keywords`), and so does the payload — **SB requires `adGroupId` and SP does not.** Measured:
#: sending SP's shape returns `207` with `KEYWORD_MISSING_AD_GROUP_ID` for every row, so the HTTP
#: status says success while nothing was applied.
WRITER_SB_KEYWORD = "sb_keyword"

#: Sponsored Brands product and category targets, and brand THEMES. A FOURTH writer.
#:
#: **Found by testing against the real report rather than assumed.** The captured 2,914-row
#: `sbTargeting` report contains four match types, and the two non-keyword ones are not a rounding
#: error: `TARGETING_EXPRESSION` is 666 rows / Rs 45,854 and `THEME` is 9 rows / Rs 1,044 — together
#: **51% of all SB spend.** Excluding them would have left half of Sponsored Brands unmanageable
#: while the tab claimed full support.
WRITER_SB_TARGET = "sb_target"

#: SB match types, lowercase as Amazon returns them from the entity API (`exact`, `phrase`) and
#: uppercase from the report. Compared upper-cased, so the case Amazon happens to use cannot
#: silently drop a row.
SB_KEYWORD_MATCH_TYPES = frozenset({"EXACT", "PHRASE", "BROAD"})

#: `TARGETING_EXPRESSION` is a product or category target (`asin="B0..."`, `category="..."`).
#: `THEME` is a Sponsored Brands brand theme (`keywords-related-to-your-brand`). Both live on
#: `/sb/targets` and both carry an editable bid — measured, all 675 such rows have one.
SB_TARGET_MATCH_TYPES = frozenset({"TARGETING_EXPRESSION", "THEME"})

#: How a row COMPETES, for the screen — as distinct from `writer_for`, which is where its bid is
#: written. **Both are shown, and that is not redundancy:** `TARGETING_EXPRESSION` occurs under both
#: ad products and routes to different APIs, so a single column cannot reveal a misrouted write.
#:
#: The template used to render EXACT, PHRASE and BROAD as one "keyword" tag, which made **1,418
#: broad-match rows indistinguishable from 14,435 exact ones** — and they compete completely
#: differently, so a bid decision on one is not a bid decision on the other.
#:
#: Every label below occurs in the live data; none of these branches is hypothetical.
MATCH_LABELS = {
    "EXACT": "exact",
    "PHRASE": "phrase",
    "BROAD": "broad",
    "TARGETING_EXPRESSION_PREDEFINED": "auto",
    "TARGETING_EXPRESSION": "product",
    "THEME": "theme",
}

#: Shown for a match type we have no name for. A question mark rather than a guess, matching
#: `writer_for` returning None: an unrecognised type is a new Amazon feature or our own typo, and a
#: plausible-looking label would hide it. A row that `writer_for` excludes still has to be
#: describable, or the skipped list cannot say what it skipped.
MATCH_UNKNOWN = "?"


def match_label(match_type, ad_product: str = AD_PRODUCT_SP) -> str:
    """A short human label for how a row competes: `exact`, `phrase`, `broad`, `auto`, `product`,
    `theme`, or `?`.

    `ad_product` is accepted but not currently consulted — the labels happen to agree across
    products. It is in the signature because `writer_for` needs it for the same input, and a caller
    holding one row should not have to remember which of the two functions cares.
    """
    return MATCH_LABELS.get(str(match_type or "").upper(), MATCH_UNKNOWN)


def writer_for(match_type, ad_product: str = AD_PRODUCT_SP) -> str | None:
    """Which endpoint owns a row: `"keyword"`, `"target"`, `"sb_keyword"`, or `None` if unrecognised.

    **`None` means the row is EXCLUDED from the run, never guessed into one.** An unknown match
    type is a new Amazon feature or a typo in our own vocabulary; guessing sends an id to the
    wrong endpoint, and the `207` that comes back reports success for the rows that worked and
    buries the failure in an `error` array. Excluding and naming it is recoverable; a silent
    misroute is not.

    **`ad_product` is required to route, and the match type alone is NOT enough.** `EXACT` is a legal
    match type for both a Sponsored Products keyword and a Sponsored Brands keyword, and they are
    written to different endpoints with different payloads. Verified the two id spaces do not collide
    (0 overlaps across 500 SP and 4,888 SB ids), so a misroute fails rather than editing the wrong
    entity — but it still fails silently inside a 207, which is why the routing is explicit.

    Defaults to `sp` so every existing caller and every pre-existing stored row keeps its meaning.
    """
    if not match_type:
        return None
    value = str(match_type).strip().upper()
    product = (ad_product or AD_PRODUCT_SP).strip().lower()

    if product == AD_PRODUCT_SB:
        if value in SB_KEYWORD_MATCH_TYPES:
            return WRITER_SB_KEYWORD
        if value in SB_TARGET_MATCH_TYPES:
            return WRITER_SB_TARGET
        return None

    if value in KEYWORD_MATCH_TYPES:
        return WRITER_KEYWORD
    if value in TARGET_MATCH_TYPES:
        return WRITER_TARGET
    return None


# ─── Who manages a campaign ──────────────────────────────────────────────────
#
# Some campaigns are optimised by something other than this app, and a bid we set in them gets
# overwritten. Excluding them is not tidiness: our rule and their optimiser would fight, and neither
# result is what anyone chose.

MANAGER_US = "us"
MANAGER_M19 = "m19"
MANAGER_AMAZON = "amazon"

#: Measured on the live account: all 4 M19 campaigns carry this in their NAME, e.g.
#: `SP -  - All products -  - exact - m19 autopilot - yQ30JKqbm+`. M19 is a third-party bid
#: automation tool the owner already runs.
M19_MARKER = "m19 autopilot"

#: Amazon's own automated campaigns, named `Adaptive Campaign - <timestamp>` (3 on this account).
AMAZON_MARKER = "adaptive campaign"


def manager_of(campaign_name) -> str:
    """Who optimises this campaign: `"us"`, `"m19"` or `"amazon"`.

    **A pure function over the NAME rather than a stored column, and that is deliberate.** The name
    is Amazon's own data and is refreshed on every fetch, so a stored flag would go stale the moment
    a campaign is renamed — and the failure mode of a stale flag here is a rule editing bids it was
    explicitly told not to. Re-deriving on every read cannot drift.

    A **convention of this account**, not an Amazon rule, which is why it is one function with its
    evidence here — exactly how `portfolio.logic._channel_of` treats the trailing ` FBA` SKU suffix.
    If M19 is dropped, deleting `M19_MARKER` restores those campaigns to the tab.

    **An unrecognised name is `us`.** A new campaign of the owner's must appear and be tunable; the
    opposite default would silently hide campaigns he had just created, which is the worse failure.

    Measured share of the account: us 16 campaigns / ₹318,036 spend / 4,948 target rows;
    m19 4 / ₹3,975 / 6,915; amazon 3 / ₹1,713 / 342. So the automated campaigns are **59% of the
    rows and 1.8% of the money** — excluding them shrinks the working set and removes the collision
    at the same time.
    """
    name = str(campaign_name or "").strip().lower()
    if M19_MARKER in name:
        return MANAGER_M19
    if AMAZON_MARKER in name:
        return MANAGER_AMAZON
    return MANAGER_US


def is_automated(campaign_name) -> bool:
    """True when something other than this app is optimising the campaign's bids."""
    return manager_of(campaign_name) != MANAGER_US


# ─── Guardrails ──────────────────────────────────────────────────────────────
#
# **Amazon accepted a ₹1,000 bid in testing.** The floor is enforced on their side; the ceiling
# is not enforced at all. These are therefore the only thing standing between a mistyped
# percentage and several hundred live bids.

#: Editable, stored as one JSON row like `portfolio_settings`, and range-checked on read as well
#: as write — the Portfolio tab shipped a `good_rating: 99` that silently zeroed a whole verdict
#: because only "is it a float" was checked.
DEFAULT_GUARDRAILS = {
    #: No resulting bid may exceed this. ₹60 is ~9x the measured median enabled bid of ₹6.39 and
    #: above the observed max of a normal manual bid, so it permits real work while refusing the
    #: ₹1,000 that Amazon would happily take.
    "max_bid": 60.0,
    #: Amazon's own floor is per marketplace and it REJECTS below it (measured at ₹0.50 on .in,
    #: which failed). Holding our own floor means those rows are excluded and reported up front
    #: rather than coming back as errors after the run.
    "min_bid": 1.0,
    #: The largest proportional move a single run may make to any bid. 25% catches "10" typed as
    #: "100" while leaving normal optimisation (5-20%) untouched.
    "max_change_pct": 25.0,
    #: A run touching more than this many rows is refused as probably over-broad.
    #:
    #: **Raised from 500 to 1000 because a legitimate rule hit it.** `spend > 100, roas < 2, -10%`
    #: matched 729 rows on the real account — real work, not a mistake — and the block forced it to
    #: be split by campaign for no safety gain. The limit exists to catch an over-BROAD rule, and
    #: every one of those rows is still previewed and individually ticked before anything is sent.
    #:
    #: 1000 rather than higher: at 2000 the limit stops discriminating, because an account-wide
    #: `spend > 0` would fit under it. The guards that actually prevent damage are `max_bid` and
    #: `max_change_pct` — this one only prevents surprise.
    "max_rows": 1000,
}

#: Bounds for each guardrail, so an edited value cannot be absurd. Same lesson as
#: `portfolio.logic.THRESHOLD_RANGES`: a finite-float check let `good_rating: 99` through, and
#: nothing could ever reach it.
GUARDRAIL_RANGES = {
    "max_bid": (1.0, 1000.0),
    "min_bid": (0.02, 100.0),
    "max_change_pct": (1.0, 100.0),
    "max_rows": (1, 20000),
}


def guardrail_error(key: str, value) -> str | None:
    """The REASON a guardrail value is refused, or None if it is acceptable.

    Returns prose rather than False so a refusal can say what the units are — "a bid ceiling of
    ₹0.10 would refuse every bid on the account" is actionable where "invalid" is not.
    """
    if key not in DEFAULT_GUARDRAILS:
        valid = ", ".join(sorted(DEFAULT_GUARDRAILS))
        return f"Unknown setting {key!r}. Valid names: {valid}."
    try:
        number = float(value)
    except (TypeError, ValueError):
        return f"{key} must be a number, got {value!r}."
    if number != number or number in (float("inf"), float("-inf")):
        return f"{key} must be a number, got {value!r}."
    low, high = GUARDRAIL_RANGES[key]
    if not (low <= number <= high):
        if key == "max_change_pct":
            return (f"{key} must be between {low:g}% and {high:g}% — a single run moving a bid "
                    f"more than that is almost always a typo, got {number:g}.")
        return f"{key} must be between {low:g} and {high:g}, got {number:g}."
    return None


def guardrails_or_default(stored: Mapping | None) -> dict:
    """Merge stored guardrails over the defaults, discarding any value that fails its range.

    **Validated on READ, not only on write.** A value already in the database — or edited by
    hand — would otherwise keep weakening the only ceiling that exists, with nothing on screen
    to show why.
    """
    merged = dict(DEFAULT_GUARDRAILS)
    for key, value in (stored or {}).items():
        if guardrail_error(key, value) is None:
            merged[key] = float(value) if key != "max_rows" else int(float(value))
    return merged


# ─── Metrics ─────────────────────────────────────────────────────────────────


def _as_float(value) -> float:
    """Coerce defensively. Postgres returns `Decimal` for `Numeric` where SQLite returns float,
    and the report returns JSON numbers; all three land here."""
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _as_int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _ratio(numerator, denominator):
    """`None` when there is no denominator — never 0.0.

    The distinction is load-bearing for exactly the reason it is in the Portfolio tab. A target
    with no spend has no ROAS; returning 0.0 would make it match `roas < 3` and sweep every
    dormant target in a 148,291-keyword account into a bid cut. `None` satisfies no comparison,
    so those rows are simply not eligible.
    """
    bottom = _as_float(denominator)
    if not bottom:
        return None
    return _as_float(numerator) / bottom


def metrics_for(row: Mapping, ad_product: str = AD_PRODUCT_SP) -> dict:
    """Normalise one report row into the fields a rule can filter on.

    Keeps Amazon's own names out of the rule vocabulary: the report says `cost`/`sales7d`, the
    owner thinks in `spend`/`sales`, and a rule written against a report column name would break
    the day Amazon renames one.

    **Handles both report shapes**, because the two ad products name the same quantities differently:
    Sponsored Products reports `sales7d`/`purchases7d`, Sponsored Brands reports plain
    `sales`/`purchases`. Read with a fallback rather than a branch, so a column Amazon renames on one
    product does not silently zero that product's sales.

    `ad_product` is carried through rather than inferred: `EXACT` is a legal match type on both, so
    the row itself cannot say which endpoint owns it.
    """
    spend = _as_float(row.get("cost"))
    sales = _as_float(row.get("sales7d") if row.get("sales7d") is not None else row.get("sales"))
    clicks = _as_int(row.get("clicks"))
    impressions = _as_int(row.get("impressions"))
    orders = _as_int(
        row.get("purchases7d") if row.get("purchases7d") is not None else row.get("purchases")
    )
    match_type = row.get("matchType") or row.get("keywordType")
    product = (ad_product or AD_PRODUCT_SP).strip().lower()
    campaign_name = row.get("campaignName") or ""

    return {
        "entity_id": str(row.get("keywordId") or row.get("targetId") or ""),
        "ad_product": product,
        "writer": writer_for(match_type, product),
        # Who optimises this campaign. Re-derived from the name on every read rather than stored,
        # so a rename cannot leave a rule editing bids it was told to leave alone.
        "manager": manager_of(campaign_name),
        "match_type": match_type,
        "text": row.get("keyword") or row.get("keywordText") or row.get("targeting") or "",
        "campaign_id": str(row.get("campaignId") or ""),
        "campaign_name": campaign_name,
        "ad_group_id": str(row.get("adGroupId") or ""),
        "ad_group_name": row.get("adGroupName") or "",
        "bid": _as_float(row.get("keywordBid") or row.get("bid")) or None,
        "spend": spend,
        "sales": sales,
        "clicks": clicks,
        "impressions": impressions,
        "orders": orders,
        # ROAS and ACOS are reciprocals, and BOTH are shown: the owner's rules are written in
        # ROAS, while the rest of this app (and the Portfolio tab) speaks ACOS. Deriving one and
        # displaying the other keeps a single source for the pair.
        "roas": _ratio(sales, spend),
        "acos": _ratio(spend, sales),
        "ctr": _ratio(clicks, impressions),
        "cvr": _ratio(orders, clicks),
        "cpc": _ratio(spend, clicks),
    }


# ─── Rule conditions ─────────────────────────────────────────────────────────

#: The fields a condition may test, with the unit each is entered in. `pct` fields are typed as
#: percentages and compared as ratios, so "acos > 50" means 0.5 — the same convention the
#: Portfolio tab's filter builder uses, because the owner uses both screens.
FIELDS = {
    "spend": {"label": "Spend", "kind": "money"},
    "sales": {"label": "Sales", "kind": "money"},
    "bid": {"label": "Bid", "kind": "money"},
    "cpc": {"label": "CPC", "kind": "money"},
    "roas": {"label": "ROAS", "kind": "number"},
    "acos": {"label": "ACOS", "kind": "pct"},
    "ctr": {"label": "CTR", "kind": "pct"},
    "cvr": {"label": "Conversion rate", "kind": "pct"},
    "clicks": {"label": "Clicks", "kind": "count"},
    "impressions": {"label": "Impressions", "kind": "count"},
    "orders": {"label": "Orders", "kind": "count"},
}

OPERATORS = {"gt": ">", "gte": ">=", "lt": "<", "lte": "<=", "eq": "="}


def condition_error(condition: Mapping) -> str | None:
    """Why a condition is unusable, or None. Checked before a run, not during it."""
    field = condition.get("field")
    if field not in FIELDS:
        return f"Unknown field {field!r}. Valid: {', '.join(sorted(FIELDS))}."
    if condition.get("op") not in OPERATORS:
        return f"Unknown comparison {condition.get('op')!r}."
    raw = condition.get("value")
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        # An empty box is not a zero. The Portfolio tab shipped this exact bug: `Number("")` is
        # 0, so a blank filter became a live `> 0` and hid half the portfolio.
        return "This condition has no value, so it cannot be applied."
    try:
        number = float(raw)
    except (TypeError, ValueError):
        return f"{field} needs a number, got {raw!r}."
    if number != number or number in (float("inf"), float("-inf")):
        return f"{field} needs a real number, got {raw!r}."
    return None


def _threshold(condition: Mapping) -> float:
    """A condition's value in the same unit as the metric — percentages become ratios."""
    number = float(condition["value"])
    return number / 100.0 if FIELDS[condition["field"]]["kind"] == "pct" else number


def matches(row_metrics: Mapping, conditions: Sequence[Mapping]) -> bool:
    """Do ALL conditions hold for this row? (ANDed, like the Portfolio filter builder.)

    **A row whose value for a tested field is `None` does NOT match.** That is the rule that
    keeps a zero-spend target out of a `roas < 3` bid cut, and it is why `_ratio` returns `None`
    rather than 0.0. An empty condition list matches nothing rather than everything — "no rule"
    must never mean "every row in the account".
    """
    if not conditions:
        return False
    for condition in conditions:
        value = row_metrics.get(condition["field"])
        if value is None:
            return False
        threshold = _threshold(condition)
        op = condition["op"]
        if op == "gt" and not value > threshold:
            return False
        if op == "gte" and not value >= threshold:
            return False
        if op == "lt" and not value < threshold:
            return False
        if op == "lte" and not value <= threshold:
            return False
        if op == "eq" and not value == threshold:
            return False
    return True


# ─── The bid action ──────────────────────────────────────────────────────────

ACTION_INCREASE_PCT = "increase_pct"
ACTION_DECREASE_PCT = "decrease_pct"
ACTION_INCREASE_ABS = "increase_abs"
ACTION_DECREASE_ABS = "decrease_abs"
ACTION_SET = "set"

ACTIONS = (ACTION_INCREASE_PCT, ACTION_DECREASE_PCT,
           ACTION_INCREASE_ABS, ACTION_DECREASE_ABS, ACTION_SET)


def new_bid(current, action: str, amount) -> float | None:
    """The bid this action produces, rounded to 2dp. `None` if it cannot be computed.

    **Rounded to 2 decimals because Amazon takes 2**: the owner's own rule gives
    `12.68 x 0.9 = 11.412`, and sending that unrounded relies on Amazon rounding it the way we
    would have. Rounding here means the preview shows exactly what will be sent.

    `None` for a row with no current bid rather than a guess from the ad group default: the ad
    group's `defaultBid` is what an inheriting target spends, but writing a bid onto the target
    CONVERTS it from inheriting to fixed, which is a structural change the owner did not ask for.
    Measured: 0 of the 299 rows matched by the real rule lacked an explicit bid, so excluding
    them costs nothing today and prevents a surprise later.
    """
    base = _as_float(current)
    if action == ACTION_SET:
        try:
            return round(float(amount), 2)
        except (TypeError, ValueError):
            return None
    if not base:
        return None
    try:
        step = float(amount)
    except (TypeError, ValueError):
        return None

    if action == ACTION_INCREASE_PCT:
        return round(base * (1 + step / 100.0), 2)
    if action == ACTION_DECREASE_PCT:
        return round(base * (1 - step / 100.0), 2)
    if action == ACTION_INCREASE_ABS:
        return round(base + step, 2)
    if action == ACTION_DECREASE_ABS:
        return round(base - step, 2)
    return None


#: Why a matched row will NOT be written. Each is shown on the preview with a count, because a
#: row silently missing from a 299-row run is indistinguishable from a bug.
SKIP_NO_BID = "no explicit bid — it inherits the ad group default"
SKIP_UNKNOWN_WRITER = "unrecognised target type, so we cannot tell which endpoint owns it"
SKIP_BELOW_FLOOR = "the new bid would fall below the marketplace minimum"
SKIP_ABOVE_CEILING = "the new bid would exceed the bid ceiling"
SKIP_NO_CHANGE = "the bid would not change"
#: **Refused here, in the pure function, rather than filtered in the UI.** A screen-level filter
#: would leave `POST /ads/apply` editable by a hand-built request, and this is the one router in the
#: app that spends money. Every path — preview, apply, a saved rule, an undo — goes through
#: `plan_run`, so this is the only place the exclusion cannot be bypassed.
SKIP_AUTOMATED = (
    "this campaign is optimised by M19 or Amazon, so a bid we set would be overwritten"
)

#: A row whose bid we already changed today. **Not a refusal — the row stays visible and is merely
#: left UNTICKED**, because there are legitimate reasons to move a bid twice (a big spender mid-sale)
#: and a row silently missing from a 1,005-row preview is indistinguishable from a bug.
#:
#: What it prevents is compounding: applying the same percentage to an already-moved bid. Measured,
#: a -10% rule run twice is **-19%**.
SKIP_CHANGED_TODAY = "the bid was already changed today, so applying again would compound it"


def plan_run(
    rows: Sequence[Mapping],
    *,
    conditions: Sequence[Mapping],
    action: str,
    amount,
    guardrails: Mapping | None = None,
    scope_campaign_ids: Sequence[str] | None = None,
    scope_ad_group_ids: Sequence[str] | None = None,
    applied_today: Mapping | None = None,
    today: str | None = None,
) -> dict:
    """Turn a rule plus a report into an auditable, un-sent PLAN.

    Returns `{"changes": [...], "skipped": [...], "blocked": str|None, "totals": {...},
    "approved_ids": [...]}`.

    ``applied_today`` is ``{entity_id: {"bid", "at", "rule", "day"}}`` from
    `repository.last_applied_bids` — the newest APPLIED ledger row per entity. It does two jobs that
    must not be confused:

    * **The bid.** A performance report does not re-issue because we changed a bid, so after a run the
      screen showed a stale one — measured on production, the report said 13.86 for a keyword Amazon
      held at 15.25. The ledger knows, so this needs no Amazon call.
    * **The guard.** Where that change happened TODAY in IST, the row is flagged and left out of
      ``approved_ids``, because applying the same percentage again compounds it: a -10% rule run twice
      is -19%.

    **The two are separate on purpose.** Yesterday's change is still the true current bid — the report
    stays stale until someone refetches — but it must not block a run today, or a rule could never
    touch the same keyword twice in its life.

    > **These two behaviours had to ship together, and the order is the whole point.** While the
    > preview computed from the stale report figure, a repeat run was accidentally idempotent
    > (13.86 x 1.10 = 15.25 both times). Correcting the displayed bid is what CREATES the compounding,
    > so a version of this function with the true bid and no guard is more dangerous than the bug it
    > fixes.

    ``today`` is the IST day to compare against, defaulting to now. A parameter so a test can pin the
    boundary without freezing the clock.

    **Nothing here contacts Amazon.** The plan is what the preview renders and what the apply
    step consumes, so what the owner approves is exactly what gets sent — the two cannot drift
    because there is only one computation.

    `blocked` is a REFUSAL of the whole run (a guardrail breach or an unusable condition), as
    distinct from `skipped`, which lists individual rows that cannot be written. The difference
    matters: a blocked run means the rule is wrong, a skipped row means that row is unsuitable.
    """
    limits = guardrails_or_default(guardrails)

    for condition in conditions or ():
        problem = condition_error(condition)
        if problem:
            return {"changes": [], "skipped": [], "blocked": problem, "totals": {}}
    if action not in ACTIONS:
        return {"changes": [], "skipped": [],
                "blocked": f"Unknown action {action!r}.", "totals": {}}

    # **A percentage move beyond the guardrail is refused BEFORE any row is considered**, so the
    # owner sees "this rule is not allowed" rather than a 299-row preview they might approve.
    if action in (ACTION_INCREASE_PCT, ACTION_DECREASE_PCT):
        try:
            step = abs(float(amount))
        except (TypeError, ValueError):
            step = None
        if step is None:
            return {"changes": [], "skipped": [],
                    "blocked": "The bid change has no value.", "totals": {}}
        if step > limits["max_change_pct"]:
            return {"changes": [], "skipped": [], "totals": {}, "blocked": (
                f"That would move bids by {step:g}%, and this account's limit is "
                f"{limits['max_change_pct']:g}% per run. Raise the limit in Settings if you "
                f"really mean it — the limit exists because Amazon accepts a ₹1,000 bid without "
                f"complaint."
            )}

    campaigns = {str(c) for c in (scope_campaign_ids or ())}
    ad_groups = {str(a) for a in (scope_ad_group_ids or ())}

    changes: list[dict] = []
    skipped: list[dict] = []
    # The IST day, resolved once: comparing against a value computed per row would let a run that
    # straddles midnight classify its own rows differently.
    this_day = today or ist_day(datetime.utcnow())
    ledger_bids = applied_today or {}

    for row in rows:
        m = row if "spend" in row and "roas" in row else metrics_for(row)

        # Scope first: cheapest test, and it is what "go inside one campaign" means.
        if campaigns and m.get("campaign_id") not in campaigns:
            continue
        if ad_groups and m.get("ad_group_id") not in ad_groups:
            continue
        if not matches(m, conditions):
            continue

        # **Automated campaigns are refused after matching, so the count is honest.** Checked here
        # rather than before `matches` deliberately: the owner should see "12 rows matched but are
        # M19's" rather than a silently smaller match count that looks like the rule not working.
        #
        # `manager` is recomputed from the name when absent, so a row assembled by an older code
        # path or a hand-built request cannot slip through without the classification.
        manager = m.get("manager") or manager_of(m.get("campaign_name"))
        if manager != MANAGER_US:
            skipped.append({**m, "manager": manager, "reason": SKIP_AUTOMATED})
            continue

        if not m.get("writer"):
            skipped.append({**m, "reason": SKIP_UNKNOWN_WRITER})
            continue
        if not m.get("bid"):
            skipped.append({**m, "reason": SKIP_NO_BID})
            continue

        # ── The TRUE current bid ──
        #
        # The report figure is stale the moment a rule runs: Amazon does not re-issue a report because
        # we changed a bid. Measured on production, the report held 13.86 for a keyword we had just
        # set to 15.25 — so a percentage applied to the report figure is a percentage of the wrong
        # number.
        #
        # **Every guard below reads `old_bid`, not `m["bid"]`.** `SKIP_NO_CHANGE` is the one that
        # would fail quietly: computed from the true bid but compared against the stale one, a row
        # already sitting at the rule's target would be reported as changing and then sent to Amazon
        # as a pointless write.
        ledger = ledger_bids.get(str(m.get("entity_id"))) or {}
        report_bid = round(_as_float(m["bid"]), 2)
        old_bid = (round(_as_float(ledger.get("bid")), 2)
                   if ledger.get("bid") is not None else report_bid)
        changed_today = bool(ledger) and ledger.get("day") == this_day

        proposed = new_bid(old_bid, action, amount)
        if proposed is None:
            skipped.append({**m, "reason": SKIP_NO_BID})
            continue
        if proposed < limits["min_bid"]:
            skipped.append({**m, "new_bid": proposed, "reason": SKIP_BELOW_FLOOR})
            continue
        if proposed > limits["max_bid"]:
            skipped.append({**m, "new_bid": proposed, "reason": SKIP_ABOVE_CEILING})
            continue
        if round(proposed, 2) == round(old_bid, 2):
            skipped.append({**m, "new_bid": proposed, "reason": SKIP_NO_CHANGE})
            continue

        changes.append({
            **m,
            "old_bid": old_bid,
            "new_bid": proposed,
            # What the REPORT said, kept so the screen can show that it was stale rather than
            # silently correcting it — the owner should be able to see why the number moved.
            "report_bid": report_bid,
            "changed_today": changed_today,
            "changed_at": ledger.get("at") or "",
            "changed_rule": ledger.get("rule") or "",
        })

    # **The default selection, computed HERE rather than in the template.**
    #
    # `POST /ads/apply` must be able to make the same judgement: a screen-level untick would leave the
    # route re-appliable from a hand-built request, and it is the only route in this app that spends
    # money. The rows are still returned — unticked and visible with their reason — because a row
    # silently missing from a 1,005-row preview is indistinguishable from a bug.
    approved_ids = [c["entity_id"] for c in changes if not c["changed_today"]]

    # **The row limit counts what will actually be SENT**, so it is measured on the appliable rows
    # rather than on every match.
    #
    # Measured on a real rule: 109 changing, 105 of them already moved today, **4 appliable**. Counting
    # all 109 would refuse a run that could only ever send 4 — and at scale, 1,100 matches with 1,050
    # already moved would be blocked by a 1,000-row limit while 50 rows went to Amazon. The limit
    # exists to bound what reaches Amazon and to prevent surprise; a refusal naming a number that
    # corresponds to nothing that would happen does neither.
    #
    # Nothing is loosened: `POST /ads/apply` re-checks the same ceiling against the rows actually
    # approved, so every row that reaches Amazon is still under it.
    #
    # The MESSAGE still names the full match count, because that is the number on screen — a refusal
    # citing a smaller figure than the table shows would read as a different bug.
    blocked = None
    if len(approved_ids) > limits["max_rows"]:
        blocked = (
            f"This rule matches {len(changes):,} rows and the limit is "
            f"{limits['max_rows']:,} per run. Narrow it with another condition, or scope it to "
            f"fewer campaigns."
        )

    return {
        "changes": changes,
        "skipped": skipped,
        "blocked": blocked,
        "approved_ids": approved_ids,
        # The same rows arranged for review. `changes` is unchanged, so every existing consumer —
        # `/ads/apply`, the ledger, the tests — is untouched by the grouping.
        "groups": group_changes(changes),
        "totals": {
            "matched": len(changes) + len(skipped),
            "changing": len(changes),
            "skipped": len(skipped),
            "spend": round(sum(c["spend"] for c in changes), 2),
            "keywords": sum(1 for c in changes if c["writer"] == WRITER_KEYWORD),
            "targets": sum(1 for c in changes if c["writer"] == WRITER_TARGET),
            "sb_keywords": sum(1 for c in changes if c["writer"] == WRITER_SB_KEYWORD),
            "sb_targets": sum(1 for c in changes if c["writer"] == WRITER_SB_TARGET),
            # How many matched rows belong to a campaign somebody else optimises, so the preview can
            # say "12 more matched but M19 manages them" rather than leaving them unexplained.
            "automated": sum(1 for s in skipped if s.get("reason") == SKIP_AUTOMATED),
            # How many arrive unticked because their bid already moved today.
            "changed_today": sum(1 for c in changes if c["changed_today"]),
        },
    }


#: Shown for a row whose campaign or ad group Amazon did not name. Labelled rather than dropped: a
#: change nobody can see is a change nobody can review, which is the rule the whole preview follows.
UNGROUPED_LABEL = "(no ad group)"


def group_changes(changes: Sequence[Mapping]) -> list[dict]:
    """`changes` arranged campaign -> ad group -> rows, with each level's own totals.

    **A re-arrangement, never a filter.** Every row appears exactly once and
    `sum(group["rows"]) == len(changes)`, which is what makes the grouped view trustworthy — a
    collapsed header that hid rows would hide bid changes.

    **The totals are computed HERE, not in the template.** A campaign header showing spend and total
    bid movement is a claim about its own rows; computed in JavaScript it can drift from the table
    beneath it, and this codebase has shipped that defect twice (the Orders tab's "86 orders beside 87
    lines", and the Portfolio parent rows that exist to prevent it). Here the number gates a live bid
    change.

    Ordered by spend descending at both levels, because "where is the money" is the question a
    1,700-row preview is being triaged for. Measured on the live account: one such rule spans **13
    campaigns and 118 ad groups**, and its largest campaign alone holds **941 rows** — which is why the
    second level exists at all rather than grouping by campaign only.

    `movement` is the NET rupee change to the bids in that group: what the header is actually claiming.
    """
    by_campaign: dict[str, dict] = {}

    for change in changes:
        campaign_id = str(change.get("campaign_id") or "")
        campaign = by_campaign.setdefault(campaign_id, {
            "campaign_id": campaign_id,
            "campaign_name": change.get("campaign_name") or campaign_id or UNGROUPED_LABEL,
            "_groups": {},
        })
        group_id = str(change.get("ad_group_id") or "")
        group = campaign["_groups"].setdefault(group_id, {
            "ad_group_id": group_id,
            "ad_group_name": change.get("ad_group_name") or group_id or UNGROUPED_LABEL,
            "entity_ids": [],
            "spend": 0.0,
            "movement": 0.0,
            "changed_today": 0,
        })
        group["entity_ids"].append(change["entity_id"])
        group["spend"] += _as_float(change.get("spend"))
        group["movement"] += _as_float(change.get("new_bid")) - _as_float(change.get("old_bid"))
        if change.get("changed_today"):
            group["changed_today"] += 1

    out = []
    for campaign in by_campaign.values():
        groups = sorted(campaign.pop("_groups").values(), key=lambda g: -g["spend"])
        for group in groups:
            group["rows"] = len(group["entity_ids"])
            group["spend"] = round(group["spend"], 2)
            group["movement"] = round(group["movement"], 2)
        out.append({
            **campaign,
            "ad_groups": groups,
            # Rolled up from the ad groups, which are rolled up from the rows — so the two levels
            # cannot disagree with each other or with the table.
            "rows": sum(g["rows"] for g in groups),
            "spend": round(sum(g["spend"] for g in groups), 2),
            "movement": round(sum(g["movement"] for g in groups), 2),
            "changed_today": sum(g["changed_today"] for g in groups),
        })
    out.sort(key=lambda c: -c["spend"])
    return out


def split_by_writer(changes: Sequence[Mapping]) -> dict[str, list[dict]]:
    """Group approved changes by the endpoint that must receive them.

    The reason this is a separate function rather than a loop at the call site: it is the last
    point where a Sponsored Products keyword, a targeting clause and a Sponsored Brands keyword are
    still in one list, and mixing them is the mistake that would be invisible. `/sp/keywords` given a
    `targetId` answers `207` with the failure inside an `error` array; `/sb/keywords` given SP's
    payload answers `207` with `KEYWORD_MISSING_AD_GROUP_ID` for every row. Both look like success at
    the HTTP level.
    """
    out: dict[str, list[dict]] = {
        WRITER_KEYWORD: [], WRITER_TARGET: [], WRITER_SB_KEYWORD: [], WRITER_SB_TARGET: [],
    }
    for change in changes:
        writer = change.get("writer")
        if writer in out:
            out[writer].append(dict(change))
    return out
