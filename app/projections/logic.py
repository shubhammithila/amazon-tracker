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
