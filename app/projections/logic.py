"""Pure rules for the Projections tab. No database, no network.

**The parent-grouping unit is the MRP sheet's own product `name`, unmerged.** Never
`app/invoice/product_families.json` — that static file is what left Triphala Sattu invisible
here in the first place, active in the sheet in two pack sizes and never added to the file.
And never `app.portfolio.logic.family_label()`: that function exists to give a multi-flavour
PARENT one shared display name for a Portfolio rollup, and `product_families.json` itself
keeps flavour variants (Cheese & Cream Chana, Nimbu Pudina Chana, Peri Peri Chana...) as
separate parents — different flavours are different recipes and different purchase decisions.
"""
from __future__ import annotations

import re
from collections.abc import Mapping


def normalize_name(name: str | None) -> str:
    """Case, space and hyphen insensitive — nothing more.

    Matches "Govind Bhog Rice" (projection_defaults.json's spelling) against
    "govind-bhog rice" (a plausible sheet spelling), but must NOT match "Gobindobhog Rice"
    against "Govind Bhog Rice" — those are different enough spellings that a false match would
    silently merge two products' purchasing config. `re.sub` strips exactly space and hyphen;
    it does not touch any other character, so a genuine spelling difference still differs.
    """
    if not name:
        return ""
    return re.sub(r"[\s-]+", "", name).casefold()


def match_defaults(parent_name: str, defaults: Mapping[str, dict]) -> dict | None:
    """The `projection_defaults.json` entry for this parent, matched by normalized name, or
    `None` if nothing matches — a genuinely new parent (Triphala Sattu) has no entry under any
    spelling. Returning `None` rather than a default dict here is deliberate: the caller,
    `build_parent_config`, is the one place that decides what a non-match means.
    """
    target = normalize_name(parent_name)
    if not target:
        return None
    for name, config in defaults.items():
        if normalize_name(name) == target:
            return config
    return None


def group_active_by_name(catalogue: Mapping[str, dict]) -> dict[str, dict]:
    """`{parent_name: {asins: [...], weights: {asin: weight}, brand: str}}` for every ACTIVE
    ASIN in the sheet's catalogue, grouped by its own `name` field — unmerged, see the module
    docstring. `catalogue` is `load_catalogue()`'s first return value:
    `{asin: {name, weight, brand, active}}`.
    """
    groups: dict[str, dict] = {}
    for asin, row in catalogue.items():
        if not row.get("active"):
            continue
        name = row.get("name") or ""
        if not name:
            continue
        group = groups.setdefault(name, {"asins": [], "weights": {}, "brand": row.get("brand") or ""})
        group["asins"].append(asin)
        group["weights"][asin] = row.get("weight") or 0
    return groups


#: The purchasing-config fields a matched (or Global Defaults) entry supplies. Kept as a tuple so
#: `build_parent_config` and its test cannot drift about which fields exist.
CONFIG_FIELDS = (
    "purchase_rate", "supplier_to_wh", "packing", "wh_to_ixd", "ixd_to_fba",
    "wh_buffer_days", "seasonal_impact",
)


def build_parent_config(
    parent_name: str, group: Mapping, defaults: Mapping[str, dict], global_defaults: Mapping,
) -> dict:
    """The purchasing config for one live parent: matched saved values, or Global Defaults
    flagged `needs_review`.

    **Never hides a parent for lack of config.** A live product with no matching entry gets
    Global Defaults rather than being dropped — the Triphala Sattu bug this whole change exists
    to fix was exactly a live product being invisible because a static file had no opinion about
    it, and repeating that here with a different static file would be the same mistake twice.
    """
    matched = match_defaults(parent_name, defaults)
    config: dict = {"parent_product": parent_name, "brand": group.get("brand") or "",
                     "needs_review": matched is None}
    source = matched if matched is not None else global_defaults
    for field in CONFIG_FIELDS:
        config[field] = source.get(field, global_defaults.get(field, 0))
    return config


def calculate_projections(
    products: list[dict], *, global_growth_rate: float, divergence_buffer_multiplier: float,
) -> list[dict]:
    """Run the reorder-point formula on each product row.

    **One formula, always applied — this is the fix for two separate bugs measured on the real
    account.**

    Bug 1: the pre-existing code only applied `seasonal_impact` and (the now-removed per-row)
    `growth_rate` to a row whose blended `daily_rate` was 0/None — a `sheet` row that HAD been
    through the weekly refresh (the normal case after the 02 Sep deploy) used its blended rate
    completely unadjusted. Whether the two factors took effect depended on an accident of which
    code path a row happened to hit, not on a decision anyone made. Now: `demand_rate` is
    computed first (the blended rate if present, `last_month_sale / 30` otherwise), and
    `seasonal_impact` / `global_growth_rate` are applied to it UNCONDITIONALLY, every time.

    Bug 2: `ideal_wh_stock` used to be `daily_rate * wh_buffer_days` alone — the supplier lead
    time (`supplier_to_wh`) never entered the warehouse reorder trigger, only `Ideal FBA`/`Lead
    Total`. A product with a 25-day supplier lead and a 10-day buffer showed a trigger that was
    blind to 25 of the 35 days it actually takes to have more stock in hand. Now
    `ideal_wh_stock = demand_rate * (supplier_to_wh + effective_wh_buffer) * seasonal * (1 +
    growth)` — the reorder point covers the FULL wait, ordering time plus safety margin, not the
    margin alone.

    `effective_wh_buffer` widens by `divergence_buffer_multiplier` when the row is already
    flagged `diverged` (its 7d/30d rates disagree beyond the saved threshold) — a volatile
    product gets more safety stock automatically the week it is detected, rather than needing the
    owner to notice the ⚠ and hand-edit `wh_buffer_days`. It never applies to `ideal_fba_stock`:
    that lead time is the internal pipeline (packing → WH→IXD → IXD→FBA), not the wait on an
    external supplier, so a demand spike does not change how long the pipeline itself takes.

    `global_growth_rate` and `divergence_buffer_multiplier` are REQUIRED keyword-only parameters,
    not read from each product dict — they are account-wide settings (the whole reason the
    growth rate stopped being a per-row column), and the caller (the router) is the one place
    that loads them from `repository.load_blend_settings`.
    """
    for p in products:
        seasonal = p.get("seasonal_impact", 1.0) or 1.0
        has_blended_rate = (p.get("daily_rate") or 0) > 0

        if has_blended_rate:
            demand_rate = p["daily_rate"]
        else:
            last_sale = p.get("last_month_sale", 0) or 0
            demand_rate = last_sale / 30

        growth_multiplier = 1 + global_growth_rate
        daily_rate = demand_rate * seasonal * growth_multiplier
        monthly_forecast = daily_rate * 30

        s2w = p.get("supplier_to_wh", 5) or 0
        pack = p.get("packing", 2) or 0
        w2i = p.get("wh_to_ixd", 10) or 0
        i2f = p.get("ixd_to_fba", 5) or 0
        total_lead = s2w + pack + w2i + i2f
        wh_buffer = p.get("wh_buffer_days", 10) or 0
        effective_wh_buffer = wh_buffer * (divergence_buffer_multiplier if p.get("diverged") else 1.0)

        ideal_fba = round(demand_rate * (pack + w2i + i2f) * seasonal * growth_multiplier, 1)
        ideal_wh = round(demand_rate * (s2w + effective_wh_buffer) * seasonal * growth_multiplier, 1)

        p["monthly_forecast"] = round(monthly_forecast, 1)
        p["daily_rate"] = round(daily_rate, 2)
        p["total_lead_time"] = total_lead
        p["effective_wh_buffer_days"] = round(effective_wh_buffer, 2)
        p["ideal_fba_stock"] = ideal_fba
        p["ideal_wh_stock"] = ideal_wh

    return products


def sales_kg_by_parent(snapshot_rows: list[dict], groups: Mapping[str, dict]) -> dict[str, float]:
    """`units_ordered x pack weight`, summed per parent name. **Never `net_units`** — see the
    module docstring's cross-reference and the test fixture's own note: `net_units` goes negative
    on a refund-heavy week (measured: 2 ASINs in a real 7-day window), and a returns problem is
    not a demand signal.

    `snapshot_rows` is Amazon's nested row shape — the same shape
    `app.portfolio.economics.fetch_economics` returns fresh and
    `app.portfolio.repository.load_snapshot` reconstructs from storage, so this function does not
    care which one supplied it. A row for an ASIN outside `groups` (discontinued, or unknown to
    the sheet) contributes to no parent — it is not this function's job to invent one.
    """
    asin_to_parent: dict[str, tuple[str, float]] = {}
    for parent, group in groups.items():
        for asin in group["asins"]:
            asin_to_parent[asin] = (parent, group["weights"].get(asin) or 0)

    totals: dict[str, float] = {}
    for row in snapshot_rows:
        asin = (row.get("childAsin") or "").strip().upper()
        mapping = asin_to_parent.get(asin)
        if not mapping:
            continue
        parent, weight = mapping
        units = int((row.get("sales") or {}).get("unitsOrdered") or 0)
        totals[parent] = totals.get(parent, 0.0) + units * weight
    return {parent: round(kg, 2) for parent, kg in totals.items()}


def blended_daily_rate(
    kg_30d: float, kg_7d: float | None, weight: float, *, divergence_fraction: float = 0.30,
) -> tuple[float, bool]:
    """`(rate, diverged)` — the daily kg/day to forecast from, and whether the 7-day and 30-day
    windows disagreed enough to flag on screen.

    **`kg_7d=None` and `kg_7d=0.0` are different facts, and this is the whole point of the
    function.** `None` means no 7-day snapshot exists yet for this parent — the window is
    missing, not zero — and the honest answer is the 30-day rate alone. `0.0` means the window
    exists and genuinely recorded no sales that week, which IS real evidence and IS blended at
    the normal weight. Collapsing the two would cut a slow mover's forecast by the blend weight
    every time the 7-day fetch simply had not run yet, which is the common case on any given day.

    `divergence_fraction` is a PARAMETER, not a hardcoded constant, because it is a saved,
    editable setting (`DEFAULT_BLEND['divergence_pct'] / 100`) — the refresh job loads it from
    storage and passes the owner's own threshold. The default of 0.30 matches `DEFAULT_BLEND`
    exactly and is only what a caller gets for free if it never loads a setting.
    """
    rate_30 = (kg_30d or 0.0) / 30
    if kg_7d is None:
        return round(rate_30, 2), False

    rate_7 = kg_7d / 7
    blended = weight * rate_7 + (1 - weight) * rate_30
    if rate_30 == 0:
        diverged = rate_7 != 0
    else:
        diverged = abs(rate_7 / rate_30 - 1) > divergence_fraction
    return round(blended, 2), diverged


#: The blend weight and divergence threshold, editable and range-checked — the same pattern
#: `app.ads.logic.DEFAULT_GUARDRAILS` / `GUARDRAIL_RANGES` / `guardrail_error` establishes, mirrored
#: with its own names because both are hardcoded to their own `PortfolioSettings.name` row and
#: neither owns the concept of "a saved, range-checked JSON setting" generally.
DEFAULT_BLEND = {
    #: How much weight the last 7 days carries against the last 30. 0.4 is a starting point
    #: measured to move real parents meaningfully (Bangla Moori-shaped: 1.74x) without letting
    #: one freak week dominate a monthly purchasing decision.
    "seven_day_weight": 0.4,
    #: The |7d/30d - 1| fraction, as a PERCENTAGE for the settings screen, above which a row is
    #: flagged diverged. 30% — smaller than the real spikes measured (58-74%) so genuine signal
    #: is not missed, larger than ordinary week-to-week noise.
    "divergence_pct": 30.0,
    #: The company's overall sales growth assumption, applied to EVERY product's forecast — one
    #: number, not per-parent. Measured against `projection_defaults.json`: 79 of 81 static
    #: entries already used 0.3, so this was already a company-wide figure typed 81 times by
    #: accident of the static file's structure, not a genuine per-product signal.
    "global_growth_rate": 0.3,
    #: How much a DIVERGED row's warehouse safety buffer widens, automatically. 1.5x means a
    #: 10-day buffer becomes 15 days the week a product's demand is flagged as having moved
    #: sharply — real protection without drastically over-buying.
    "divergence_buffer_multiplier": 1.5,
}

#: Bounds for each blend setting. Same lesson as `app.ads.logic.GUARDRAIL_RANGES`: a
#: `good_rating: 99`-shaped mistake here (`seven_day_weight: 99`) would mean "ignore the 30-day
#: figure and treat one week as the whole history" — silently, with nothing to catch it.
BLEND_RANGES = {
    "seven_day_weight": (0.0, 1.0),
    "divergence_pct": (1.0, 200.0),
    "global_growth_rate": (0.0, 3.0),
    #: Floor is 1.0, NOT 0.0 — a value below 1 would SHRINK a volatile product's buffer, which is
    #: the exact inversion `good_rating: 99` already taught this codebase to guard against on
    #: read as well as write.
    "divergence_buffer_multiplier": (1.0, 5.0),
}


def blend_setting_error(key: str, value) -> str | None:
    """The REASON a blend setting is refused, or `None` if acceptable. Prose, not a bare False,
    so a refusal can say what the units are — the same shape as `app.ads.logic.guardrail_error`.
    """
    if key not in DEFAULT_BLEND:
        valid = ", ".join(sorted(DEFAULT_BLEND))
        return f"Unknown setting {key!r}. Valid names: {valid}."
    try:
        number = float(value)
    except (TypeError, ValueError):
        return f"{key} must be a number, got {value!r}."
    if number != number or number in (float("inf"), float("-inf")):
        return f"{key} must be a number, got {value!r}."
    low, high = BLEND_RANGES[key]
    if not (low <= number <= high):
        if key == "seven_day_weight":
            return (f"{key} must be between {low:g} and {high:g} — it is a fraction of the "
                     f"blend, got {number:g}.")
        return f"{key} must be between {low:g} and {high:g}, got {number:g}."
    return None


def blend_or_default(stored: Mapping | None) -> dict:
    """Merge stored blend settings over the defaults, discarding any value that fails its range.

    **Validated on READ, not only on write** — a value already in the database, or edited by
    hand outside the app, must not keep silently distorting every parent's forecast.
    """
    merged = dict(DEFAULT_BLEND)
    for key, value in (stored or {}).items():
        if blend_setting_error(key, value) is None:
            merged[key] = float(value)
    return merged


def hidden_parent_names(stored_names: set[str], live_groups: Mapping[str, dict]) -> list[str]:
    """Which stored parents are no longer active in the sheet, sorted, for the screen's
    hidden-parents note. **Named, never a bare count** — a parent silently missing from a
    91-row list is indistinguishable from a bug, the same rule the Shipment tab's catalogue
    diff follows.
    """
    return sorted(stored_names - set(live_groups))
