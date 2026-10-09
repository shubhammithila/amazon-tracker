"""Assemble the Customer value sub-tab. Reads stored rows only; never calls Amazon."""
from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import date, timedelta

from app.portfolio.logic import CATEGORY_UNCLASSIFIED
from app.repeat import logic, repository, service, value

#: History begins here; "new" means never seen since this day (the owner's definition), so the
#: earliest months over-count new customers — the screen names them rather than hiding them.
HISTORY_START = "2026-01-01"
#: Months this soon after history began are flagged: a customer seen first in them may well have
#: bought before 1 January.
SHORT_HISTORY_MONTHS = 3


def _empty(brand):
    return {"as_of": None, "brand": brand, "brands": [], "rows": [], "total": {},
            "cac_months": [], "grid": [], "categories": [], "horizons": list(value.HORIZONS),
            "priced_from": None, "min_customers": value.MIN_CUSTOMERS}


#: The payload is ~10 s of work on the t2.micro (every category is its own scope) and changes only
#: when new lines, prices or ad figures land, so it is kept until one of those moves.
_CACHE: dict = {}


async def _data_version(db) -> tuple:
    from sqlalchemy import func, select
    from app.models import CustomerOrderLine, EconomicsDaily, RepeatRefresh
    q = lambda col: select(func.max(col))  # noqa: E731
    return ((await db.execute(q(RepeatRefresh.id))).scalar(),
            (await db.execute(q(CustomerOrderLine.fetched_at))).scalar(),
            (await db.execute(select(func.count()).select_from(CustomerOrderLine)
                              .where(CustomerOrderLine.revenue.is_(None)))).scalar(),
            (await db.execute(q(EconomicsDaily.fetched_at))).scalar())


async def build_payload(db, brand: str | None, today: date) -> dict:
    brand = brand or service.DEFAULT_BRAND
    version = (today, await _data_version(db))
    if _CACHE.get("version") != version:
        _CACHE.clear()                   # one data version at a time; every brand under it
        _CACHE["version"] = version
    if brand not in _CACHE:
        _CACHE[brand] = await _build(db, brand, today)
    return _CACHE[brand]


async def _build(db, brand: str, today: date) -> dict:
    runs = await repository.done_runs(db)
    if not runs:
        return _empty(brand)
    catalogue, _, _ = await service.load_catalogue()
    latest = min(max(date.fromisoformat(b) for _, b in runs), today - timedelta(days=1))
    as_of = latest - timedelta(days=service.SHIP_LAG_DAYS)
    ctx = await service.prepare(db, catalogue, brand, HISTORY_START)
    priced = await repository.priced_from(db, HISTORY_START)
    econ, econ_days = await repository.econ_by_month(db)

    def child_key(child, parent):
        return ctx.flavour_of.get(child) or parent

    def scoped(p):
        return ctx.in_brand(p)

    async def figures(in_scope):
        # CPU-bound, so it runs in a worker thread: on the event loop it froze every other page of
        # the app for the length of the build (measured 50-137 s before the speed-ups).
        return await asyncio.to_thread(value.compute, ctx.lines, in_scope, as_of,
                                       date.fromisoformat(priced) if priced else None,
                                       econ, econ_days, child_key=child_key)

    result = await figures(scoped)
    for row in result["rows"]:
        p = row["parent_asin"]
        row["product"] = ctx.names.get(p, p)
        row["brand"] = ctx.brand_of.get(p, "")
        row["category"] = ctx.category_of.get(p, CATEGORY_UNCLASSIFIED)
    result["rows"].sort(key=lambda r: (-r["new_customers"], r["product"].casefold()))

    # Per category: computed as its own scope, so a category's LTV and CAC count each customer once
    # (acquired by any of its products) — never an average of the rows below it.
    per_category = defaultdict(int)
    for r in result["rows"]:
        per_category[r["category"]] += 1
    categories = []
    for cat, count in per_category.items():
        part = await figures(lambda p, cat=cat: scoped(p) and ctx.category_of.get(p) == cat)
        categories.append({"category": cat, "products": count, "total": part["total"],
                           "grid": part["grid"]})
    categories.sort(key=lambda c: (-c["total"]["new_customers"], c["category"]))
    short_until = value.add_months(HISTORY_START[:7], SHORT_HISTORY_MONTHS)
    return {"as_of": as_of.isoformat(), "brand": brand,
            "brands": [service.ALL_BRANDS] + sorted(set(ctx.brand_of.values())),
            "rows": result["rows"], "total": result["total"], "cac_months": result["cac_months"],
            "grid": result["grid"], "categories": categories,
            "horizons": list(value.HORIZONS), "priced_from": priced,
            "history_start": HISTORY_START, "short_history_before": short_until,
            "min_customers": value.MIN_CUSTOMERS}



def build_table(payload, ids, category):
    """The Customer value table as `portfolio.export` writes it: the screen's rows, in its order."""
    from app.portfolio.export import Column, Row, Table
    cols = ([Column("product", "Product", "text"), Column("category", "Category", "text"),
             Column("new", "New customers", "int")]
            + [Column(f"ltv-{n}", f"LTV {n}d", "money") for n in value.HORIZONS]
            + [Column("cac", "CAC", "money"), Column("ltv_cac", "LTV:CAC", "times"),
               Column("payback", "Payback days", "int")])

    def values(r, name=None):
        out = {"product": name or r.get("product"), "category": r.get("category"),
               "new": r.get("new_customers"), "cac": r.get("cac"), "ltv_cac": r.get("ltv_cac"),
               "payback": r.get("payback_days")}
        for n in value.HORIZONS:
            out[f"ltv-{n}"] = (r.get("ltv") or {}).get(str(n))
        return out

    by_id = {r["parent_asin"]: r for r in payload.get("rows") or []}
    chosen = ([by_id[i] for i in dict.fromkeys(ids) if i in by_id] if ids is not None
              else [r for r in payload.get("rows") or [] if not category or r["category"] == category])
    group = next((c for c in payload.get("categories") or [] if c["category"] == category), None)
    total = group["total"] if group else payload.get("total") or {}
    label = f"{group['category'] if group else payload.get('brand') or 'Brand'} — all products"
    return Table(cols, [Row("sku", 0, False, values(r)) for r in chosen], label, values(total, label))
