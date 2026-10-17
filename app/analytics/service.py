"""The Analytics payload. Reads stored months only; never calls Amazon.

**Each month is rolled up by the Profit tab's own `logic.portfolio`**, fed the month's rows in
Amazon's shape. So a product row here IS a Profit row (parent ASIN, flavours split, Active flag,
family name) and its Net is the Profit tab's Net, by construction rather than by a second copy of the
rules. Everything after that — month-on-month, the growth split, categories, concentration, the
funnel — is `analytics.logic` over those rows.
"""
from __future__ import annotations

import asyncio
import calendar
import time
from datetime import date

from app import ist
from app.analytics import logic, repository

ALL_BRANDS = "All brands"
CACHE_SECONDS = 600
_CACHE: dict = {}


def _size_entry(size: dict, with_flavour: bool) -> dict:
    from app.portfolio import logic as pf_logic
    return {"label": pf_logic._size_label(size, with_flavour=with_flavour), "sales": size["sales"],
            "units": size["units_ordered"], "kg": size.get("weight_ordered_kg")}


def _compute_base(months_rows, traffic, catalogue, flavours, categories, include_inactive) -> dict:
    """{key: product series} for every product that sold in any stored month. CPU only."""
    from app.portfolio import logic as pf_logic

    products: dict[str, dict] = {}
    child_key: dict[str, str] = {}
    for month in sorted(months_rows):
        result = pf_logic.portfolio(months_rows[month], catalogue, {}, {}, include_inactive=include_inactive,
                                    flavour_keys=flavours)
        for p in result["parents"]:
            many = len({str(s.get("product") or "") for s in p["sizes"]}) > 1
            prod = products.setdefault(p["parent_asin"], {"key": p["parent_asin"], "months": {}})
            # The newest month names the product, the way the Profit tab names it today.
            prod.update(product=p["product"], brand=p.get("brand") or "", inactive=bool(p.get("inactive")),
                        category=pf_logic.category_of(p, categories))
            prod["months"][month] = {
                "sales": p["sales"], "units": p["units_ordered"], "net_units": p["units"],
                "kg": p.get("weight_ordered_kg"), "net": p["net"], "fees": p["fees_total"],
                "refunded": p["refunded"], "ad_spend": p["ad_spend"],
                "sizes": {s["asin"]: _size_entry(s, many) for s in p["sizes"]},
            }
            for s in p["sizes"]:
                child_key[s["asin"]] = p["parent_asin"]
    # Traffic rows map to the same product rows: the child's row from economics, else its flavour or
    # parent. A child with traffic but no sales in any month has no row and is left out.
    for month, rows in traffic.items():
        for asin, t in rows.items():
            key = child_key.get(asin) or flavours.get(asin) or t.get("parent_asin")
            if key in products:
                products[key].setdefault("traffic", {}).setdefault(month, []).append(t)
    return products


def _days_in(month: str) -> int:
    return calendar.monthrange(int(month[:4]), int(month[5:7]))[1]


def _sum(entries, key):
    vals = [e.get(key) for e in entries if e is not None]
    vals = [v for v in vals if v is not None]
    return round(sum(vals), 2) if vals else None


def _month_totals(entries: list[dict]) -> dict:
    sales = _sum(entries, "sales")
    known_kg = all(e.get("kg") is not None for e in entries if e.get("units"))
    out = {"sales": sales, "units": _sum(entries, "units"), "kg": _sum(entries, "kg") if known_kg else None,
           "net": _sum(entries, "net"), "fees": _sum(entries, "fees"), "refunded": _sum(entries, "refunded"),
           "ad_spend": _sum(entries, "ad_spend")}
    out.update(net_pct=logic.pct(out["net"], sales), fees_pct=logic.pct(out["fees"], sales),
               refunds_pct=logic.pct(out["refunded"], sales), tacos=logic.pct(out["ad_spend"], sales))
    return out


def _payload(base: dict, brand: str, category: str | None, sb_days: dict, today: date) -> dict:
    current = today.strftime("%Y-%m")
    rows = [p for p in base.values()
            if (brand == ALL_BRANDS or p["brand"] == brand) and (not category or p["category"] == category)]
    all_months = sorted({m for p in base.values() for m in p["months"]})
    months = logic.month_range(all_months[0], all_months[-1]) if all_months else []
    complete = [m for m in months if m < current]
    mtd = current if current in months else None

    totals, splits = {}, {}
    for i, m in enumerate(months):
        entries = [p["months"].get(m) for p in rows if p["months"].get(m)]
        t = _month_totals(entries)
        t.update(logic.weighted_rates([r for p in rows for r in (p.get("traffic") or {}).get(m, [])]))
        t["top10_share"] = logic.concentration({p["key"]: (p["months"].get(m) or {}).get("sales") for p in rows})
        held = (sb_days.get(m) or {}).get("days", 0)
        whole = _days_in(m) if m != current else max(today.day - 1, 1)
        t["sb"] = "full" if held >= whole else ("partial" if held else "none")
        prev = months[i - 1] if i else None
        t["mom"] = logic.growth(t["sales"], totals[prev]["sales"]) if prev and m in complete else None
        totals[m] = t
        if prev and m in complete:
            splits[m] = logic.add_splits(
                logic.growth_split((p["months"].get(prev) or {}).get("sizes", {}),
                                   (p["months"].get(m) or {}).get("sizes", {})) for p in rows)

    cats = {}
    for p in rows:
        for m, e in p["months"].items():
            c = cats.setdefault(p["category"], {})
            c.setdefault(m, []).append(e)
    categories = []
    for name, by_month in cats.items():
        series = {}
        for i, m in enumerate(months):
            t = _month_totals(by_month.get(m, []))
            prev = months[i - 1] if i else None
            t["share"] = logic.pct(t["sales"], totals[m]["sales"])
            t["mom"] = (logic.growth(t["sales"], _month_totals(by_month.get(prev, []))["sales"])
                        if prev and m in complete else None)
            series[m] = t
        last = complete[-1] if complete else None
        categories.append({"category": name, "months": series,
                           "last_sales": (series.get(last) or {}).get("sales") if last else None})
    categories.sort(key=lambda c: -(c["last_sales"] or 0))

    last = complete[-1] if complete else None
    prior = complete[-2] if len(complete) > 1 else None
    six = complete[-7] if len(complete) > 6 else (complete[0] if complete else None)
    table = []
    for p in rows:
        cur, before = p["months"].get(last) or {}, p["months"].get(prior) or {}
        series = {m: {**{k: v for k, v in e.items() if k != "sizes"},
                      "net_pct": logic.pct(e["net"], e["sales"]),
                      **logic.weighted_rates((p.get("traffic") or {}).get(m, []))}
                  for m, e in p["months"].items()}
        sizes = {}
        for m, e in p["months"].items():
            for asin, s in e["sizes"].items():
                entry = sizes.setdefault(asin, {"asin": asin, "label": s["label"], "months": {}})
                entry["months"][m] = {"asp": logic.pct(s["sales"], s["units"]), "units": s["units"]}
        top = max(sizes.values(), key=lambda s: (s["months"].get(last) or {}).get("units") or 0, default=None)
        prices = [v["asp"] for v in (top["months"].values() if top else []) if v["asp"] is not None]
        traffic_last = logic.weighted_rates((p.get("traffic") or {}).get(last, []))
        table.append({
            "key": p["key"], "product": p["product"], "brand": p["brand"], "category": p["category"],
            "inactive": p["inactive"],
            "sales": cur.get("sales"), "mom": logic.growth(cur.get("sales"), before.get("sales")),
            "net_pct": logic.pct(cur.get("net"), cur.get("sales")),
            "net_pct_change": (logic.pct(cur.get("net"), cur.get("sales")) - logic.pct(before.get("net"), before.get("sales"))
                               if cur.get("sales") and before.get("sales") else None),
            "kg": cur.get("kg"),
            "price_pack": top["label"] if top else None,
            "price_now": (top["months"].get(last) or {}).get("asp") if top else None,
            "price_6m": (top["months"].get(six) or {}).get("asp") if top and six else None,
            "price_low": min(prices) if prices else None, "price_high": max(prices) if prices else None,
            "sessions": traffic_last["sessions"], "conversion": traffic_last["conversion"],
            "buy_box": traffic_last["buy_box"],
            "series": series, "sizes": sorted(sizes.values(), key=lambda s: s["label"]),
        })
    table.sort(key=lambda r: -(r["sales"] or 0))

    return {
        "months": months, "complete_months": complete, "last_month": last,
        "mtd": {"month": mtd, "days": today.day - 1} if mtd else None,
        "totals": totals, "growth": splits, "categories": categories, "products": table,
        "brand": brand, "category": category,
        "brands": [ALL_BRANDS] + sorted({p["brand"] for p in base.values() if p["brand"]}),
    }


async def build(db, *, brand: str | None = None, category: str | None = None,
                include_inactive: bool = False, today: date | None = None) -> dict:
    from app.portfolio import repository as pf_repo
    from app.shipment import catalogue, repository as ship_repo

    today = today or ist.today()
    version = (today, await repository.data_version(db))
    if _CACHE.get("version") != version:
        _CACHE.clear()
        _CACHE["version"] = version
    hit = _CACHE.get(include_inactive)
    if not hit or time.monotonic() - hit[0] > CACHE_SECONDS:
        months_rows = await repository.load_months(db)
        traffic = await repository.load_traffic(db)
        products, _warning, source = await catalogue.load_catalogue()
        flavours = await pf_repo.flavour_keys(db, products)
        categories = {r.product_key: r.priority for r in await ship_repo.load_categories(db)}
        sb_days = await repository.sb_by_month(db)
        base = await asyncio.to_thread(_compute_base, months_rows, traffic, products, flavours,
                                       categories, include_inactive)
        hit = (time.monotonic(), base, sb_days, source)
        _CACHE[include_inactive] = hit
    _, base, sb_days, source = hit
    payload = _payload(base, brand or ALL_BRANDS, category, sb_days, today)
    payload.update(include_inactive=include_inactive, catalogue_source=source,
                   last_run=await repository.last_run(db))
    return payload
