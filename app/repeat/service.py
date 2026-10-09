"""Assemble the Repeat tab's payload. Reads stored rows only; never calls Amazon."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta

from app.portfolio import repository as pf_repository
from app.portfolio.logic import CATEGORY_UNCLASSIFIED, _first_category
from app.repeat import logic, repository, value
from app.shipment import repository as ship_repository
from app.shipment.catalogue import load_catalogue
from app.shipment.logic import CATEGORY_LABELS

#: An order bought on day D can ship D+1..D+3, so the newest days of a fetch are incomplete.
SHIP_LAG_DAYS = 3
#: Below this FBA share a product's repeat % reads low: Easy Ship orders carry no customer key.
FBA_PARTIAL_BELOW = 0.6
DEFAULT_BRAND = "Mithila Foods"
#: The brand selector's "everything" choice: every brand's products, one unique-customer total.
ALL_BRANDS = "All brands"
#: Newest stored day older than this many days before yesterday: the data is stale and the screen
#: says so, whatever the last run said. The nightly run normally keeps it at 0.
STALE_AFTER_DAYS = 2


def _empty(brand, last):
    return {"as_of": None, "history_from": None, "brand": brand, "brands": [], "windows": {},
            "total": {}, "rows": [], "categories": [], "last_refresh": last,
            "min_cohort": logic.MIN_COHORT,
            "fba_partial_below": FBA_PARTIAL_BELOW}


@dataclass
class Context:
    """What both Portfolio customer sub-tabs read: ONE loader, so a product's key, name, brand and
    category are identical on Repeat customers and Customer value."""
    lines: list
    flavour_of: dict
    brand_of: dict
    names: dict
    category_of: dict
    everything: bool
    brand: str

    def in_brand(self, p) -> bool:
        return self.everything or self.brand_of.get(p) == self.brand


async def prepare(db, catalogue, brand: str, since: str) -> Context:
    lines = await repository.load_lines(db, since)
    # A multi-flavour parent becomes one row per flavour, keyed EXACTLY as the Profit view keys it
    # (one shared map), so the same flavour reads the same on every sub-tab.
    flavour_of = await pf_repository.flavour_keys(db, catalogue)
    for line in lines:
        line["parent_asin"] = flavour_of.get(line["child_asin"]) or line["parent_asin"]
    everything = brand == ALL_BRANDS
    units = defaultdict(lambda: defaultdict(int))
    brand_of: dict[str, str] = {}
    for line in lines:
        units[line["parent_asin"]][line["child_asin"]] += line["units"]
        rec = catalogue.get(line["child_asin"]) or {}
        if rec.get("brand"):
            brand_of.setdefault(line["parent_asin"], rec["brand"])
    names = logic.name_parents({
        p: [(catalogue.get(c) or {}).get("name") or c
            for c, _ in sorted(kids.items(), key=lambda kv: (-kv[1], kv[0]))]
        for p, kids in units.items()})
    # The Profit view's categories, read the same way: the owner's stored choice for the parent
    # name or any of its children's catalogue names, never a keyword guess. One vocabulary, so
    # "Sattu" here holds exactly the products "Sattu" holds there.
    stored = {row.product_key: row.priority for row in await ship_repository.load_categories(db)}
    category_of = {}
    for p, kids in units.items():
        if not (everything or brand_of.get(p) == brand):
            continue
        candidates = [names.get(p, p)] + [(catalogue.get(c) or {}).get("name") or "" for c in kids]
        priority = _first_category(candidates, stored)
        category_of[p] = (CATEGORY_LABELS.get(priority, CATEGORY_LABELS[6])
                          if priority is not None else CATEGORY_UNCLASSIFIED)
    return Context(lines, flavour_of, brand_of, names, category_of, everything, brand)


async def build_payload(db, brand: str | None, today: date) -> dict:
    brand = brand or DEFAULT_BRAND
    runs = await repository.done_runs(db)
    # Only a run that fetched RECENT days can say the screen's data is at risk; see last_run.
    last = await repository.last_run(
        db, ending_from=(today - timedelta(days=STALE_AFTER_DAYS + 1)).isoformat())
    if not runs:
        return _empty(brand, last)
    catalogue, _, _ = await load_catalogue()
    latest = min(max(date.fromisoformat(b) for _, b in runs), today - timedelta(days=1))
    as_of = latest - timedelta(days=SHIP_LAG_DAYS)
    stale_days = max(0, ((today - timedelta(days=1)) - latest).days)
    history_from = logic.covered_from(runs, as_of)
    since = (as_of - timedelta(days=max(logic.WINDOWS) * 2 + logic.PERIOD_DAYS)).isoformat()
    ctx = await prepare(db, catalogue, brand, since)
    lines, flavour_of, brand_of, names = ctx.lines, ctx.flavour_of, ctx.brand_of, ctx.names
    category_of, everything = ctx.category_of, ctx.everything
    reorder = value.reorder_days(lines)

    # "All brands" is totalled as one brand, so its top row counts each customer once overall.
    totals_by = {p: ALL_BRANDS for p in brand_of} if everything else brand_of
    m = logic.metrics(logic.build_orders(lines), totals_by, as_of, history_from,
                      group_of=category_of)
    fba_child, all_child = await repository.units_by_child(
        db, (as_of - timedelta(days=89)).isoformat(), as_of.isoformat())
    # Keyed by Amazon's parent (or the flavour), so a child that sold only by Easy Ship still
    # counts in its product's denominator — that is exactly the sale the FBA share is about.
    fba, allc = defaultdict(int), defaultdict(int)
    for (parent, child), v in fba_child.items():
        fba[flavour_of.get(child) or parent] += v
    for (parent, child), v in all_child.items():
        allc[flavour_of.get(child) or parent] += v

    def fba_share(p):
        # Units can net NEGATIVE (a refund adjustment: Ragi Thekua read -1 over 90 days), so a
        # share is only shown when there were real units to share; never a "-0.0%".
        if allc.get(p, 0) <= 0:
            return None
        return min(1.0, fba.get(p, 0) / allc[p])

    def label(p):
        return names.get(p, p)

    rows = []
    for p, per in m["parents"].items():
        if not everything and brand_of.get(p) != brand:
            continue
        w = {str(n): {"buyers": c["buyers"], "same_pct": logic.pct(c["same"], c["buyers"]),
                      "units_pct": logic.units_pct(c["repeat_units"], c["units"], c["buyers"]),
                      "came_from_pct": logic.pct(c["came_from"], c["buyers"]),
                      "went_on_pct": logic.pct(c["went_on"], c["buyers"])}
             for n, c in per.items()}
        flows = {str(n): {k: [{"product": label(q), "customers": v} for q, v in f[k]]
                          for k in ("came_from", "went_on")}
                 for n, f in m["flows"][p].items()}
        b = m["basket"].get(p, {"orders": 0, "multi": 0, "with": []})
        rows.append({
            "parent_asin": p, "product": label(p), "brand": brand_of.get(p, ""),
            "category": category_of.get(p, CATEGORY_UNCLASSIFIED),
            "fba_share": fba_share(p),
            # Median days between a customer's orders of this product; a dash below 20 gaps.
            "reorder_days": (reorder.get(p) or {}).get("median"),
            "reorder_gaps": (reorder.get(p) or {}).get("gaps", 0),
            "w": w, "flows": flows,
            "basket": {"orders": b["orders"], "multi_pct": logic.pct(b["multi"], b["orders"]),
                       "with": [{"product": label(q), "orders": v} for q, v in b["with"]]},
        })
    rows.sort(key=lambda r: (-max((v["buyers"] for v in r["w"].values()), default=0),
                             r["product"].casefold()))
    def group_total(counts):
        return {str(n): {"buyers": c["buyers"], "repeat_pct": logic.pct(c["repeat"], c["buyers"]),
                         "units_pct": logic.units_pct(c["repeat_units"], c["units"], c["buyers"])}
                for n, c in counts.items()}

    total = group_total(m["brands"].get(ALL_BRANDS if everything else brand, {}))
    # Per category, unique customers like the brand total, so a category's figure is NOT the sum
    # or average of its rows. Biggest first by 90-day buyers ("where are the customers").
    per_category = defaultdict(int)
    for r in rows:
        per_category[r["category"]] += 1
    categories = sorted(
        ({"category": cat, "products": count, "total": group_total(m["groups"].get(cat, {}))}
         for cat, count in per_category.items()),
        key=lambda c: (-((c["total"].get("90") or {}).get("buyers") or 0), c["category"]))
    windows = {str(n): {"period": [w["period"][0].isoformat(), w["period"][1].isoformat()],
                        "available": w["available"], "reason": w["reason"]}
               for n, w in m["windows"].items()}
    return {"as_of": as_of.isoformat(),
            "history_from": history_from.isoformat() if history_from else None,
            "brand": brand, "brands": [ALL_BRANDS] + sorted(set(brand_of.values())),
            "windows": windows,
            "total": total, "rows": rows, "categories": categories, "last_refresh": last,
            "newest_day": latest.isoformat(),
            "stale_days": stale_days if stale_days > STALE_AFTER_DAYS else 0,
            "min_cohort": logic.MIN_COHORT, "fba_partial_below": FBA_PARTIAL_BELOW}
