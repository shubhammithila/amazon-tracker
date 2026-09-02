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
    "wh_buffer_days", "seasonal_impact", "growth_rate",
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


def calculate_projections(products: list[dict]) -> list[dict]:
    """Run projection formulas on each product row.

    **Moved from `app/routers/projections.py`, and the daily-rate source is NOT unchanged —
    this is the one piece of arithmetic this feature actually exists to change.** The
    pre-existing formula derived `daily_rate` from `last_month_sale * seasonal * (1 + growth)`
    every time, which would have silently discarded the whole 7d/30d blend: a `sheet`-sourced row
    already carries its blended `daily_rate`, computed once a week by
    `app.projections.refresh.run` from real units_ordered data, and recomputing it here from
    `last_month_sale` alone would have made the entire weekly-blend feature invisible on screen.

    So: a row whose `sales_source == "sheet"` and already has a non-null `daily_rate` (the
    normal case after at least one weekly refresh) keeps that rate and derives
    `monthly_forecast = daily_rate * 30` FROM it — seasonal/growth are not applied a second
    time, because `blended_daily_rate` has no notion of them and double-applying a growth factor
    on top of a rate already measured from real sales would inflate the forecast for no reason.
    A `manual` row, or a `sheet` row that has never been refreshed yet (`daily_rate` is 0 or
    `None`, e.g. immediately after `build_current_rows` creates a brand-new parent), falls back
    to the original `last_month_sale`-driven formula, since a manual edit only ever supplies
    `last_month_sale` and has no blended rate to read.
    """
    for p in products:
        seasonal = p.get("seasonal_impact", 1.0) or 1.0
        growth = p.get("growth_rate", 0.3) or 0.0
        has_blended_rate = (
            p.get("sales_source") == "sheet" and (p.get("daily_rate") or 0) > 0
        )

        if has_blended_rate:
            daily_rate = p["daily_rate"]
            monthly_forecast = daily_rate * 30
        else:
            last_sale = p.get("last_month_sale", 0) or 0
            monthly_forecast = last_sale * seasonal * (1 + growth)
            daily_rate = monthly_forecast / 30

        s2w = p.get("supplier_to_wh", 5) or 0
        pack = p.get("packing", 2) or 0
        w2i = p.get("wh_to_ixd", 10) or 0
        i2f = p.get("ixd_to_fba", 5) or 0
        total_lead = s2w + pack + w2i + i2f
        wh_buffer = p.get("wh_buffer_days", 10) or 0

        ideal_fba = round(daily_rate * total_lead, 1)
        ideal_wh = round(daily_rate * wh_buffer, 1)

        current_fba = p.get("current_fba_stock", 0) or 0
        current_wh = p.get("current_wh_stock", 0) or 0

        shipment_alert = round(ideal_fba - current_fba, 1)
        reorder_alert = round(ideal_fba + ideal_wh - current_fba - current_wh, 1)

        purchase_rate = p.get("purchase_rate", 0) or 0
        ideal_stock_value = round((ideal_fba + ideal_wh) * purchase_rate, 0)
        current_stock_value = round((current_fba + current_wh) * purchase_rate, 0)

        inventory_days = round(current_fba / daily_rate, 1) if daily_rate > 0 else 0

        p["monthly_forecast"] = round(monthly_forecast, 1)
        p["daily_rate"] = round(daily_rate, 2)
        p["total_lead_time"] = total_lead
        p["ideal_fba_stock"] = ideal_fba
        p["ideal_wh_stock"] = ideal_wh
        p["shipment_alert"] = shipment_alert
        p["reorder_alert"] = reorder_alert
        p["ideal_stock_value"] = ideal_stock_value
        p["current_stock_value"] = current_stock_value
        p["inventory_days"] = inventory_days

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
}

#: Bounds for each blend setting. Same lesson as `app.ads.logic.GUARDRAIL_RANGES`: a
#: `good_rating: 99`-shaped mistake here (`seven_day_weight: 99`) would mean "ignore the 30-day
#: figure and treat one week as the whole history" — silently, with nothing to catch it.
BLEND_RANGES = {
    "seven_day_weight": (0.0, 1.0),
    "divergence_pct": (1.0, 200.0),
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
