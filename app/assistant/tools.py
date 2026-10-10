"""The assistant's tools: small, read-only slices of the payloads the three sub-tabs already render.

**The server computes every number; the model only chooses which slice to ask for and phrases the
answer.** Each tool takes a payload built by the same function behind its tab (`_dashboard`,
`repeat.service.build_payload`, `repeat.value_service.build_payload`), so an answer cannot disagree
with the screen. A filtered set's total is summed here from its rows with every percentage
recomputed from the sums, never averaged, the rule `_sum_sizes` follows.

**Small on purpose.** At most `MAX_LIMIT` rows and only the fields that answer questions, rounded:
the whole Profit payload is ~40k tokens, a slice ~1-2k.

**Nothing here writes.** No tool reaches a decision, a refresh, a bid or Amazon, and a test pins the
tool list so one cannot be added by accident.
"""
from __future__ import annotations

from typing import Awaitable, Callable

from app.portfolio import logic as pf_logic

DEFAULT_LIMIT = 15
MAX_LIMIT = 40

PROFIT_SORT = ["sales", "ad_spend", "net", "net_pct", "tacos", "acos", "refunds_pct", "fees_pct",
               "units_ordered", "net_units", "weight_ordered_kg", "net_weight_kg", "net_per_kg",
               "returns_pct", "rating", "product"]
REPEAT_SORT = ["buyers", "same_pct", "units_pct", "came_from_pct", "went_on_pct", "fba_share",
               "reorder_days", "product"]
VALUE_SORT = ["new_customers", "ltv_30", "ltv_60", "ltv_90", "ltv_180", "cac", "ltv_cac",
              "payback_days", "product"]
WINDOWS = ["90", "60", "30"]


def _money(v):
    return None if v is None else round(float(v))


def _pct(v):
    """A fraction as a percentage with one decimal. None stays None: a dash, never 0%."""
    return None if v is None else round(float(v) * 100, 1)


def _num(v, places=2):
    return None if v is None else round(float(v), places)


def _limit(inp) -> int:
    try:
        n = int(inp.get("limit") or DEFAULT_LIMIT)
    except (TypeError, ValueError):
        n = DEFAULT_LIMIT
    return max(1, min(MAX_LIMIT, n))


def _matches(text: str, search: str | None) -> bool:
    return not search or search.casefold().strip() in (text or "").casefold()


def _named(product: str, wanted) -> bool:
    """`products` filter: keep a row whose name equals, or contains, one of the names asked for."""
    if not wanted:
        return True
    name = (product or "").casefold()
    return any(str(w).casefold().strip() and str(w).casefold().strip() in name for w in wanted)


def _sorted(rows: list[dict], key: str, ascending: bool) -> list[dict]:
    """Sort on `key`; a missing value sorts LAST in both directions, as on the screen."""
    if key == "product":
        return sorted(rows, key=lambda r: r["product"].casefold(), reverse=not ascending)
    have = [r for r in rows if r.get(key) is not None]
    none = [r for r in rows if r.get(key) is None]
    have.sort(key=lambda r: r[key], reverse=not ascending)
    return have + none


# ── Profit ──────────────────────────────────────────────────────────────────────────────────────

def profit_category(parent: dict, categories: dict) -> str:
    """The owner's stored category for a parent, matched on its own or any size's name, exactly as
    the Profit tab's category strip does."""
    from app.shipment.logic import CATEGORY_LABELS
    names = [parent.get("product")] + [s.get("product") for s in parent.get("sizes") or []]
    priority = pf_logic._first_category(names, categories)
    if priority is None:
        return pf_logic.CATEGORY_UNCLASSIFIED
    return CATEGORY_LABELS.get(priority, CATEGORY_LABELS[6])


def _profit_row(p: dict, categories: dict) -> dict:
    return {
        "product": p["product"], "brand": p.get("brand"), "category": profit_category(p, categories),
        "verdict": p.get("verdict"),
        "verdict_group": pf_logic.VERDICT_GROUPS.get(p.get("verdict"), pf_logic.GROUP_MAINTAIN),
        "verdict_reason": p.get("verdict_reason"),
        "owner_decision": p.get("decision") or None,
        "inactive": bool(p.get("inactive")),
        "sales": _money(p.get("sales")), "ad_spend": _money(p.get("ad_spend")),
        "net": _money(p.get("net")),
        "net_pct": _pct(p.get("net_pct")), "refunds_pct": _pct(p.get("refunds_pct")),
        "fees_pct": _pct(p.get("fees_pct")), "tacos": _pct(p.get("tacos")),
        "acos": "no attributed sales" if p.get("acos_infinite") else _pct(p.get("acos")),
        "units_ordered": p.get("units_ordered"), "net_units": p.get("units"),
        "weight_ordered_kg": _num(p.get("weight_ordered_kg"), 1),
        "net_weight_kg": _num(p.get("weight_kg"), 1), "net_per_kg": _num(p.get("net_per_kg")),
        "returns_pct": _pct(p.get("returns_pct")),
        "rating": p.get("rating"), "rating_count": p.get("rating_count"),
    }


def _known_sum(parents: list[dict], key: str):
    """Sum of a weight over the rows that HAVE one; None when none do, never 0 kg (the totals row's
    rule: a product with no known pack weight leaves the weight, not the sum, blank)."""
    vals = [float(p[key]) for p in parents if p.get(key) is not None]
    return sum(vals) if vals else None


def _sum_profit(parents: list[dict]) -> dict:
    """The total of a filtered set, exactly as the Profit tab's totals row computes it: money and units
    summed, every percentage recomputed from the rupee sums, Net ₹/kg = net of the weighed sizes over
    their net weight. Given so the model never adds rows up itself."""
    s = {k: sum(float(p.get(k) or 0) for p in parents)
         for k in ("sales", "ad_spend", "net", "refunded", "fees_total")}
    s["net_weighed"] = sum(float(p.get("net_weighed") or 0) for p in parents if p.get("weight_kg") is not None)
    ratio = (lambda a: a / s["sales"]) if s["sales"] else (lambda a: None)
    net_kg = _known_sum(parents, "weight_kg")
    return {"products": len(parents), "sales": _money(s["sales"]), "ad_spend": _money(s["ad_spend"]),
            "net": _money(s["net"]), "net_pct": _pct(ratio(s["net"])),
            "tacos": _pct(ratio(s["ad_spend"])), "refunds_pct": _pct(ratio(s["refunded"])),
            "fees_pct": _pct(ratio(s["fees_total"])),
            "units_ordered": sum(int(p.get("units_ordered") or 0) for p in parents),
            "net_units": sum(int(p.get("units") or 0) for p in parents),
            "weight_ordered_kg": _num(_known_sum(parents, "weight_ordered_kg"), 1),
            "net_weight_kg": _num(net_kg, 1),
            "net_per_kg": _num(s["net_weighed"] / net_kg) if net_kg else None}


def _profit_header(data: dict) -> dict:
    window = list(data.get("window") or [])
    comp = data.get("completeness") or {}
    out = {"source": "Profit", "window": window}
    if data.get("include_inactive"):
        out["includes_inactive"] = True
    elif data.get("inactive_hidden_parents"):
        out["hidden_inactive_products"] = (
            f"{data['inactive_hidden_parents']} product(s) marked inactive in the MRP sheet are hidden, "
            "as on screen; ask again with include_inactive=true to see them.")
    if not comp.get("complete"):
        out["incomplete"] = (f"This range cannot be summed: {comp.get('missing_count', '?')} "
                             "day(s) of data are missing, so no figures are shown.")
    return out


def profit_products(data: dict, categories: dict, inp: dict) -> dict:
    sort = inp.get("sort_by") if inp.get("sort_by") in PROFIT_SORT else "sales"
    rows = []
    chosen = []
    for p in data.get("parents") or []:
        r = _profit_row(p, categories)
        if inp.get("category") and r["category"].casefold() != str(inp["category"]).casefold():
            continue
        if inp.get("brand") and (r["brand"] or "").casefold() != str(inp["brand"]).casefold():
            continue
        if inp.get("verdict_group") and r["verdict_group"].casefold() != str(inp["verdict_group"]).casefold():
            continue
        if inp.get("verdict") and (r["verdict"] or "").casefold() != str(inp["verdict"]).casefold():
            continue
        names = " ".join([p["product"]] + [s.get("product") or "" for s in p.get("sizes") or []])
        if not _matches(names, inp.get("search")) or not _named(names, inp.get("products")):
            continue
        rows.append(r)
        chosen.append(p)
    rows = _sorted(rows, sort, bool(inp.get("ascending")))
    limit = _limit(inp)
    return {**_profit_header(data), "matching_products": len(rows), "shown": min(limit, len(rows)),
            "total_of_matching": _sum_profit(chosen), "account_total": _sum_profit(data.get("parents") or []),
            "rows": rows[:limit]}


def profit_product(data: dict, categories: dict, inp: dict) -> dict:
    name = str(inp.get("product") or "").strip()
    found = [p for p in data.get("parents") or []
             if _matches(" ".join([p["product"]] + [s.get("product") or "" for s in p.get("sizes") or []]), name)]
    exact = [p for p in found if p["product"].casefold() == name.casefold()]
    found = exact or found
    if not found:
        return {**_profit_header(data), "error": f"No product matches {name!r} in this window."}
    if len(found) > 1:
        return {**_profit_header(data), "several_match": [p["product"] for p in found[:MAX_LIMIT]]}
    p = found[0]
    sizes = [{
        "size_product": s.get("product"), "pack_kg": s.get("weight"), "asin": s.get("asin"),
        "active_in_sheet": s.get("active", True),
        "sales": _money(s.get("sales")), "ad_spend": _money(s.get("ad_spend")), "net": _money(s.get("net")),
        "net_pct": _pct(s.get("net_pct")), "tacos": _pct(s.get("tacos")),
        "acos": "no attributed sales" if s.get("acos_infinite") else _pct(s.get("acos")),
        "units_ordered": s.get("units_ordered"), "net_units": s.get("units"),
        "net_per_kg": _num(s.get("net_per_kg")), "returns_pct": _pct(s.get("returns_pct")),
    } for s in p.get("sizes") or []]
    fees = {k: _money(v) for k, v in (p.get("fees") or {}).items()}
    return {**_profit_header(data), "product": _profit_row(p, categories), "fees_by_type": fees,
            "sizes": sizes}


def profit_categories(data: dict, categories: dict, inp: dict) -> dict:
    cats = (data.get("category_totals") or {}).get("categories") or []
    by_cat: dict[str, list] = {}
    for p in data.get("parents") or []:
        by_cat.setdefault(profit_category(p, categories), []).append(p)
    return {**_profit_header(data), "account_total": _sum_profit(data.get("parents") or []),
            "categories": [{
                **_sum_profit(by_cat.get(c["category"], [])),
                "category": c["category"], "products": c.get("products"),
                "products_named": (c.get("products_named") or [])[:20],
            } for c in cats]}


# ── Repeat customers ────────────────────────────────────────────────────────────────────────────

def _repeat_header(data: dict, window: str) -> dict:
    w = (data.get("windows") or {}).get(window) or {}
    out = {"source": "Repeat customers", "brand": data.get("brand"), "data_to": data.get("as_of"),
           "follow_up_days": int(window), "cohort_period": w.get("period")}
    if w and not w.get("available"):
        out["window_unavailable"] = w.get("reason") or "not enough history"
    return out


def _repeat_total(t: dict, window: str) -> dict:
    c = (t or {}).get(window) or {}
    return {"buyers": c.get("buyers"), "same_pct": _pct(c.get("repeat_pct")),
            "units_pct": _pct(c.get("units_pct"))}


def repeat_products(data: dict, inp: dict) -> dict:
    window = str(inp.get("window") or "90")
    window = window if window in WINDOWS else "90"
    sort = inp.get("sort_by") if inp.get("sort_by") in REPEAT_SORT else "buyers"
    available = ((data.get("windows") or {}).get(window) or {}).get("available", True)
    partial_below = data.get("fba_partial_below") or 0.6
    rows = []
    for r in data.get("rows") or []:
        if inp.get("category") and (r.get("category") or "").casefold() != str(inp["category"]).casefold():
            continue
        if not _matches(r["product"], inp.get("search")) or not _named(r["product"], inp.get("products")):
            continue
        c = ((r.get("w") or {}).get(window) or {}) if available else {}
        rows.append({
            "product": r["product"], "category": r.get("category"),
            "buyers": c.get("buyers"), "same_pct": _pct(c.get("same_pct", c.get("repeat_pct"))),
            "units_pct": _pct(c.get("units_pct")), "came_from_pct": _pct(c.get("came_from_pct")),
            "went_on_pct": _pct(c.get("went_on_pct")), "fba_share": _pct(r.get("fba_share")),
            "partial_fba_data": r.get("fba_share") is not None and r["fba_share"] < partial_below,
            "reorder_days": r.get("reorder_days"),
        })
    rows = _sorted(rows, sort, bool(inp.get("ascending")))
    limit = _limit(inp)
    total = data.get("total")
    scope = data.get("brand")
    if inp.get("category"):
        cat = next((c for c in data.get("categories") or []
                    if c["category"].casefold() == str(inp["category"]).casefold()), None)
        total, scope = (cat or {}).get("total"), (cat or {}).get("category", inp["category"])
    return {**_repeat_header(data, window), "matching_products": len(rows),
            "scope_total": {"scope": scope, **_repeat_total(total, window)},
            "rows": rows[:limit]}


def repeat_flows(data: dict, inp: dict) -> dict:
    window = str(inp.get("window") or "90")
    window = window if window in WINDOWS else "90"
    name = str(inp.get("product") or "").strip()
    found = [r for r in data.get("rows") or [] if _matches(r["product"], name)]
    exact = [r for r in found if r["product"].casefold() == name.casefold()]
    found = exact or found
    if not found:
        return {**_repeat_header(data, window), "error": f"No product matches {name!r}."}
    if len(found) > 1:
        return {**_repeat_header(data, window), "several_match": [r["product"] for r in found[:MAX_LIMIT]]}
    r = found[0]
    flows = (r.get("flows") or {}).get(window) or {}
    basket = r.get("basket") or {}
    return {**_repeat_header(data, window), "product": r["product"],
            "came_from": (flows.get("came_from") or [])[:8],
            "went_on_to": (flows.get("went_on") or [])[:8],
            "bought_together": {"orders_last_period": basket.get("orders"),
                                "share_with_another_product_pct": _pct(basket.get("multi_pct")),
                                "with": (basket.get("with") or [])[:8]}}


# ── Customer value ──────────────────────────────────────────────────────────────────────────────

def _value_figures(r: dict) -> dict:
    ltv = r.get("ltv") or {}
    return {"new_customers": r.get("new_customers"),
            **{f"ltv_{n}": _money(ltv.get(str(n))) for n in (30, 60, 90, 180)},
            "cac": _money(r.get("cac")), "ltv_cac": _num(r.get("ltv_cac")),
            "payback_days": r.get("payback_days")}


def _value_header(data: dict) -> dict:
    return {"source": "Customer value", "brand": data.get("brand"), "data_to": data.get("as_of"),
            "new_since": data.get("history_start"), "prices_loaded_from": data.get("priced_from")}


def value_products(data: dict, inp: dict) -> dict:
    sort = inp.get("sort_by") if inp.get("sort_by") in VALUE_SORT else "new_customers"
    rows = []
    for r in data.get("rows") or []:
        if inp.get("category") and (r.get("category") or "").casefold() != str(inp["category"]).casefold():
            continue
        if not _matches(r["product"], inp.get("search")) or not _named(r["product"], inp.get("products")):
            continue
        rows.append({"product": r["product"], "category": r.get("category"), **_value_figures(r)})
    rows = _sorted(rows, sort, bool(inp.get("ascending")))
    total, scope = data.get("total") or {}, data.get("brand")
    if inp.get("category"):
        cat = next((c for c in data.get("categories") or []
                    if c["category"].casefold() == str(inp["category"]).casefold()), None)
        total, scope = (cat or {}).get("total") or {}, (cat or {}).get("category", inp["category"])
    months = [{"month": m["month"], "cac": _money(m.get("cac")), "new_customers": m.get("cac_customers")}
              for m in data.get("cac_months") or []]
    return {**_value_header(data), "matching_products": len(rows),
            "scope_total": {"scope": scope, **_value_figures(total)},
            "cac_by_month": months, "rows": rows[:_limit(inp)]}


def cohorts(data: dict, inp: dict) -> dict:
    grid, scope = data.get("grid") or [], data.get("brand")
    if inp.get("category"):
        cat = next((c for c in data.get("categories") or []
                    if c["category"].casefold() == str(inp["category"]).casefold()), None)
        grid, scope = (cat or {}).get("grid") or [], (cat or {}).get("category", inp["category"])
    return {**_value_header(data), "scope": scope,
            "note": "Month 0 is the first-order month. Later cells: share of that month's new customers "
                    "who ordered again in that calendar month, and cumulative ₹ per customer to its end. "
                    "null = month not complete yet.",
            "cohorts": [{
                "first_month": g["month"], "customers": g["customers"],
                "still_buying_pct": [None if c is None else _pct(c.get("active_pct")) for c in g["cells"]],
                "spend_per_customer": [None if c is None else _money(c.get("revenue_per_customer"))
                                       for c in g["cells"]],
            } for g in grid]}


# ── The tool list the model sees ────────────────────────────────────────────────────────────────

_WINDOW_PROPS = {
    "days": {"type": "integer", "description": "Last N days ending yesterday (1-90). Omit to use the window on screen."},
    "start": {"type": "string", "description": "Custom range start, YYYY-MM-DD (with end)."},
    "end": {"type": "string", "description": "Custom range end, YYYY-MM-DD (with start)."},
}
_COMMON = {
    "category": {"type": "string", "description": "Sattu, Chana, Flours, Rice, Seeds, Rest or Unclassified."},
    "search": {"type": "string", "description": "Substring of a product name."},
    "ascending": {"type": "boolean", "description": "Lowest first. Default highest first."},
    "limit": {"type": "integer", "description": f"Rows to return, 1-{MAX_LIMIT}. Default {DEFAULT_LIMIT}."},
    "products": {"type": "array", "items": {"type": "string"},
                 "description": "Only these products (names, matched as substrings). Use it to look up "
                                "products found on another tab."},
}
_BRAND = {"brand": {"type": "string", "description": "Brand name, or 'All brands'. Omit for the brand on screen."}}
_REPEAT_WINDOW = {"window": {"type": "string", "enum": WINDOWS, "description": "Follow-up window in days. Default 90."}}


def _spec(name, description, props, required=()):
    return {"toolSpec": {"name": name, "description": description,
                         "inputSchema": {"json": {"type": "object", "properties": props,
                                                  "required": list(required)}}}}


SPECS = [
    _spec("profit_products",
          "Profit tab: products (parent rows, one per flavour) for a date window with sales, ad spend, "
          "net, Net %, TACOS, ACOS, refunds %, Amazon fees %, units, weight, Net ₹/kg, returns %, "
          "rating, verdict and its reason. Filter, sort, limit. Also returns the total of the matching "
          "products and the account total.",
          {**_WINDOW_PROPS, **_COMMON,
           "brand": {"type": "string"},
           "include_inactive": {"type": "boolean",
                                "description": "Also products marked inactive in the MRP sheet (hidden on screen by default)."},
           "verdict_group": {"type": "string", "enum": list(pf_logic.GROUP_ORDER)},
           "verdict": {"type": "string", "enum": list(pf_logic.VERDICT_ORDER)},
           "sort_by": {"type": "string", "enum": PROFIT_SORT}}),
    _spec("profit_product",
          "Profit tab: ONE product in detail, with each pack size's figures and the fees by type.",
          {**_WINDOW_PROPS, "product": {"type": "string"},
           "include_inactive": {"type": "boolean"}}, required=["product"]),
    _spec("profit_categories",
          "Profit tab: sales, ad spend, net, Net % and TACOS per category, plus the account total.",
          dict(_WINDOW_PROPS)),
    _spec("repeat_products",
          "Repeat customers tab: per product, of customers who first bought it in a 30-day period, the "
          "share who bought it AGAIN within the window (same_pct), the share of its units bought by "
          "repeaters (units_pct, Brand Analytics' measure), came_from/went_on %, FBA share and median "
          "reorder days. Plus the scope's unique-customer total.",
          {**_BRAND, **_REPEAT_WINDOW, **_COMMON, "sort_by": {"type": "string", "enum": REPEAT_SORT}}),
    _spec("repeat_flows",
          "Repeat customers tab: for ONE product, which of our products its buyers came from, went on "
          "to, and bought in the same order.",
          {**_BRAND, **_REPEAT_WINDOW, "product": {"type": "string"}}, required=["product"]),
    _spec("value_products",
          "Customer value tab: per first product (the product that brought a new customer in), new "
          "customers, LTV at 30/60/90/180 days (what they paid us, ex-GST), CAC, LTV:CAC and payback "
          "days. Plus the scope total and CAC by month.",
          {**_BRAND, **_COMMON, "sort_by": {"type": "string", "enum": VALUE_SORT}}),
    _spec("cohorts",
          "Customer value tab: the cohort heat map. Per first-order month: customers, % still buying in "
          "each later month, and cumulative spend per customer.",
          {**_BRAND, "category": _COMMON["category"]}),
]
TOOL_NAMES = tuple(s["toolSpec"]["name"] for s in SPECS)


class Sources:
    """The three payload builders, injected so tests need no database."""

    def __init__(self, profit: Callable[[dict], Awaitable[tuple[dict, dict]]],
                 repeat: Callable[[str | None], Awaitable[dict]],
                 value: Callable[[str | None], Awaitable[dict]]):
        self.profit, self.repeat, self.value = profit, repeat, value


async def run(name: str, inp: dict, sources: Sources, context: dict) -> dict:
    """Execute one tool call. The screen's brand and window fill what the model left out."""
    inp = dict(inp or {})
    if name.startswith("profit_"):
        if not (inp.get("days") or (inp.get("start") and inp.get("end"))):
            if context.get("start") and context.get("end"):
                inp["start"], inp["end"] = context["start"], context["end"]
        data, categories = await sources.profit(inp)
        if data.get("error"):
            return {"error": data["error"]}
        fn = {"profit_products": profit_products, "profit_product": profit_product,
              "profit_categories": profit_categories}[name]
        out = fn(data, categories, inp)
        # A product the owner names is worth finding even when the sheet marks it inactive: the
        # screen hides it, but "why is X bad?" deserves X, flagged, rather than "not found".
        if (name == "profit_product" and out.get("error") and not inp.get("include_inactive")):
            data, categories = await sources.profit({**inp, "include_inactive": True})
            retry = fn(data, categories, inp)
            if not retry.get("error"):
                out = retry
        return out
    brand = inp.get("brand") or context.get("brand") or None
    if name in ("repeat_products", "repeat_flows"):
        data = await sources.repeat(brand)
        return (repeat_products if name == "repeat_products" else repeat_flows)(data, inp)
    if name in ("value_products", "cohorts"):
        data = await sources.value(brand)
        return (value_products if name == "value_products" else cohorts)(data, inp)
    return {"error": f"Unknown tool {name!r}."}
