"""Assemble the live Projections table. Reads only: no Amazon call, no write.

**The product list IS the Profit tab's.** Both windows are built by the Profit tab's own
`_dashboard` (the last 30 days and the last 7, each ending yesterday), so the products, their
flavour split, the MRP sheet's Active flag and the "Show N inactive" toggle behave identically on
both tabs, and the Profit tab's "Total stock to hold" column is this module's figure for the same
row id.

**Sales are live.** The Profit tab stores sales per day and re-reads them every night at 07:30 IST,
so this page is current the moment that job lands. There is no upload and no weekly job any more.
"""
from __future__ import annotations

import json
import time
from functools import lru_cache
from pathlib import Path
from datetime import date, datetime

from sqlalchemy import func, select

from app import ist
from app.models import EconomicsDaily, FbaInventoryRefresh, PortfolioSettings, ProjectionRow
from app.portfolio import economics
from app.portfolio import logic as pf_logic
from app.projections import logic, repository
from app.shipment.logic import CATEGORY_LABELS

#: Category display order, the Shipment tab's packing order: Sattu, Chana, Flours, Rice, Seeds,
#: Rest, then anything unclassified.
CATEGORY_ORDER = [CATEGORY_LABELS[k] for k in sorted(CATEGORY_LABELS)] + [pf_logic.CATEGORY_UNCLASSIFIED]
CACHE_SECONDS = 300
#: An FBA stock reading older than this gets a banner (the nightly job runs every 24 hours).
STALE_HOURS = 36
#: Stock held for a product with no row is named only from this many kg.
UNMAPPED_MIN_KG = 5

_CACHE: dict = {}


_DEFAULTS_PATH = Path(__file__).resolve().parent.parent / "invoice" / "projection_defaults.json"


@lru_cache(maxsize=1)
def _defaults_file() -> dict:
    """The hand-kept per-product lead times, used for a product with nothing saved yet."""
    try:
        return json.loads(_DEFAULTS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def windows(today: date) -> dict:
    return {"30": economics.window_for(today, 30), "7": economics.window_for(today, 7)}


async def _version(db) -> tuple:
    """What the figures depend on, so the cache is dropped the moment any of it moves."""
    econ = (await db.execute(select(func.max(EconomicsDaily.fetched_at)))).scalar()
    rows = (await db.execute(select(func.max(ProjectionRow.updated_at), func.count(ProjectionRow.id)))).one()
    stored = (await db.execute(select(PortfolioSettings.value_json)
                               .where(PortfolioSettings.name == repository.SETTING_NAME))).scalar()
    stock_run = (await db.execute(select(func.max(FbaInventoryRefresh.id)))).scalar()
    return (str(econ), str(rows[0]), rows[1], stored, stock_run)


def _config_for(row: dict, stored_by_key: dict, stored_by_name: dict) -> tuple[dict, str, int | None]:
    """(values, source, pre-key row id). Saved by key, else a pre-key row of the same name, else
    `projection_defaults.json` by name, else the global defaults."""
    if row["key"] in stored_by_key:
        return stored_by_key[row["key"]], "saved", None
    named = stored_by_name.get(logic.normalize_name(row["product"]))
    if named is not None:
        return named, "saved", named["id"]
    matched = logic.match_defaults(row["product"], _defaults_file())
    if matched is not None:
        return matched, "defaults", None
    return logic.GLOBAL_DEFAULTS, "global", None


def _rows(d30: dict, d7: dict | None, stored: list[dict], categories: dict, settings: dict) -> list[dict]:
    by_key = {r["product_key"]: r for r in stored if r.get("product_key")}
    by_name = {}
    for r in stored:
        if not r.get("product_key"):
            by_name.setdefault(logic.normalize_name(r["parent_product"]), r)
    seven = ({p["parent_asin"]: p.get("weight_ordered_kg") for p in d7["parents"]} if d7 else None)

    out = []
    for p in d30.get("parents") or []:
        row = {"key": p["parent_asin"], "product": p["product"], "brand": p.get("brand") or "",
               "category": pf_logic.category_of(p, categories), "inactive": bool(p.get("inactive")),
               "kg_30d": p.get("weight_ordered_kg")}
        # Absent from a complete 7-day window = sold nothing these 7 days: a real zero, blended.
        # No 7-day window at all = unknown: the 30-day rate alone.
        row["kg_7d"] = None if seven is None else (seven.get(row["key"]) or 0.0)
        if row["kg_30d"] is None:
            row["kg_7d"] = None
        config, source, pre_key_id = _config_for(row, by_key, by_name)
        for field in logic.EDITABLE:
            value = config.get(field)
            row[field] = logic.GLOBAL_DEFAULTS[field] if value is None else value
        row["seasonal_impact"] = float(row["seasonal_impact"])
        row["config_source"] = source
        row["pre_key_row"] = pre_key_id
        row.update(logic.compute(row, settings))
        out.append(row)
    rank = {c: i for i, c in enumerate(CATEGORY_ORDER)}
    out.sort(key=lambda r: (rank.get(r["category"], len(rank)), -(r["kg_30d"] or 0), r["product"].casefold()))
    return out


async def _fba_units(db) -> tuple[dict[str, int] | None, dict]:
    """(units per child ASIN from the latest GOOD stock run, or None when there is none yet; what
    the newest run said, so a failure can be shown while the previous figures stand)."""
    from app.inventory import logic as inv_logic, repository as inv_repo

    good = await inv_repo.latest_good(db)
    newest = await inv_repo.latest(db)
    info = {"as_of": good.finished_at.isoformat() if good and good.finished_at else None,
            "status": newest.status if newest else None,
            "error": newest.error if newest and newest.status != "done" else None}
    if good is None:
        return None, info
    return inv_logic.units_by_asin(await inv_repo.rows_for(db, good.id)), info


async def _fba_by_key(db, units: dict[str, int] | None):
    """({row key: kg, or None when a held pack has no weight}, unknown-weight child ASINs, the
    catalogue, {child: row key}).

    A child maps to its row the way the Profit tab maps it: its flavour id, else Amazon's parent,
    else itself. Weighed from the MRP sheet, NOT from the 30-day size rows, so a pack size holding
    stock that did not sell this month still counts."""
    from app.portfolio import repository as pf_repo
    from app.shipment import catalogue

    if units is None:
        return {}, [], {}, {}
    products, _warning, _source = await catalogue.load_catalogue()
    flavours = await pf_repo.flavour_keys(db, products)
    parents = await pf_repo.parent_of_children(db)
    kg: dict[str, float | None] = {}
    unknown: list[str] = []
    keys: dict[str, str] = {}
    for child, n in units.items():
        key = keys[child] = flavours.get(child) or parents.get(child) or child
        weight = (products.get(child) or {}).get("weight")
        if not n:
            kg.setdefault(key, 0.0)
            continue
        if not weight:
            # Unknown weight is a dash for the whole row, never a silently smaller figure.
            unknown.append(child)
            kg[key] = None
            continue
        if key in kg and kg[key] is None:
            continue
        kg[key] = round((kg.get(key) or 0.0) + n * float(weight), 3)
    return kg, sorted(unknown), products, keys


def _stale(as_of: str | None) -> bool:
    if not as_of:
        return True
    return (datetime.utcnow() - datetime.fromisoformat(as_of)).total_seconds() > STALE_HOURS * 3600


def _cards(rows: list[dict]) -> list[dict]:
    cards = []
    for cat in CATEGORY_ORDER:
        members = [r for r in rows if r["category"] == cat]
        if members:
            cards.append({"category": cat, **logic.sum_rows(members)})
    return cards


async def build(db, *, include_inactive: bool = False, today: date | None = None) -> dict:
    """The whole Projections payload. Cached for a few minutes per data version."""
    from app.routers import portfolio as pf_router
    from app.shipment import repository as ship_repository

    today = today or ist.today()
    version = (today, await _version(db))
    if _CACHE.get("version") != version:
        _CACHE.clear()               # one data version at a time; both toggles live under it
        _CACHE["version"] = version
    hit = _CACHE.get(include_inactive)
    if hit and time.monotonic() - hit[0] < CACHE_SECONDS:
        return hit[1]

    settings = await repository.load_settings(db)
    w = windows(today)
    d30 = await pf_router._dashboard(db, w["30"], include_inactive=include_inactive)
    d7 = await pf_router._dashboard(db, w["7"], include_inactive=include_inactive)
    complete_30 = bool((d30.get("completeness") or {}).get("complete"))
    complete_7 = bool((d7.get("completeness") or {}).get("complete"))
    categories = {r.product_key: r.priority for r in await ship_repository.load_categories(db)}
    stored = await repository.load_rows(db)

    rows = _rows(d30, d7 if complete_7 else None, stored, categories, settings) if complete_30 else []

    # Stock at FBA or on its way, from the latest GOOD Inventory API run. No run yet = a dash on
    # every row, never "buy everything".
    units, run_info = await _fba_units(db)
    fba_kg, unknown, products, keys = await _fba_by_key(db, units)
    shown_all = {r["key"] for r in rows}
    for r in rows:
        r["fba_kg"] = None if units is None else fba_kg.get(r["key"], 0.0)
        r["to_purchase"] = logic.to_purchase(r["total_stock"], r["fba_kg"])
    # Stock held for a product with no row (no sales in 30 days) is NAMED, never silently dropped.
    # Only once the table exists: with the 30 days not all stored there are no rows, and naming
    # every product held would bury the banner that actually explains the empty table.
    unmapped_kg: dict[str, float] = {}
    for child, key in (keys.items() if complete_30 else ()):
        weight = (products.get(child) or {}).get("weight")
        if key in shown_all or not units[child] or not weight:
            continue
        name = (products.get(child) or {}).get("name") or child
        unmapped_kg[name] = round(unmapped_kg.get(name, 0.0) + units[child] * float(weight), 1)
    # A few hundred grams of a retired pack is not worth a banner; a real holding is.
    unmapped = [{"product": n, "kg": k} for n, k in sorted(unmapped_kg.items(), key=lambda x: -x[1])
                if k >= UNMAPPED_MIN_KG]
    # The Profit tab keeps an inactive product on screen when a decision is recorded against it;
    # stock is never planned for one, so here every inactive row waits behind the toggle.
    kept_inactive = 0
    if not include_inactive:
        kept_inactive = sum(1 for r in rows if r["inactive"])
        rows = [r for r in rows if not r["inactive"]]
    payload = {
        "rows": rows,
        "categories": _cards(rows),
        "total": logic.sum_rows(rows),
        "settings": settings,
        "setting_ranges": logic.SETTING_RANGES,
        "edit_ranges": logic.EDIT_RANGES,
        "windows": {"30": list(w["30"]), "7": list(w["7"])},
        "complete_30": complete_30,
        "complete_7": complete_7,
        "missing_30": (d30.get("completeness") or {}).get("missing_count") or 0,
        "include_inactive": include_inactive,
        "inactive_hidden": (d30.get("inactive_hidden_parents") or 0) + kept_inactive,
        "unknown_weight": [r["product"] for r in rows if r["kg_30d"] is None],
        "fba_stock": {**run_info, "stale": _stale(run_info["as_of"]), "unmapped": unmapped,
                      "unknown_weight": unknown},
    }
    _CACHE[include_inactive] = (time.monotonic(), payload)
    return payload


async def stock_by_key(db, *, today: date | None = None) -> dict[str, dict]:
    """{Profit-tab row id: {"total_stock", "to_purchase"}, kg}, for the Profit tab's columns.
    Inactive products are included so every row the Profit tab may show has its figures."""
    data = await build(db, include_inactive=True, today=today)
    return {r["key"]: {"total_stock": r["total_stock"], "to_purchase": r["to_purchase"]}
            for r in data["rows"]}

