"""Assemble the Repeat tab's payload. Reads stored rows only; never calls Amazon."""
from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta

from app.repeat import logic, repository
from app.shipment.catalogue import load_catalogue

#: An order bought on day D can ship D+1..D+3, so the newest days of a fetch are incomplete.
SHIP_LAG_DAYS = 3
#: Below this FBA share a product's repeat % reads low: Easy Ship orders carry no customer key.
FBA_PARTIAL_BELOW = 0.6
DEFAULT_BRAND = "Mithila Foods"


def _empty(brand, last):
    return {"as_of": None, "history_from": None, "brand": brand, "brands": [], "windows": {},
            "total": {}, "rows": [], "last_refresh": last, "min_cohort": logic.MIN_COHORT,
            "fba_partial_below": FBA_PARTIAL_BELOW}


async def build_payload(db, brand: str | None, today: date) -> dict:
    brand = brand or DEFAULT_BRAND
    runs = await repository.done_runs(db)
    last = await repository.last_run(db)
    if not runs:
        return _empty(brand, last)
    catalogue, _, _ = await load_catalogue()
    latest = min(max(date.fromisoformat(b) for _, b in runs), today - timedelta(days=1))
    as_of = latest - timedelta(days=SHIP_LAG_DAYS)
    history_from = logic.covered_from(runs, as_of)
    since = (as_of - timedelta(days=max(logic.WINDOWS) * 2 + logic.PERIOD_DAYS)).isoformat()
    lines = await repository.load_lines(db, since)

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

    m = logic.metrics(logic.build_orders(lines), brand_of, as_of, history_from)
    fba, allc = await repository.units_by_parent(
        db, (as_of - timedelta(days=89)).isoformat(), as_of.isoformat())

    def label(p):
        return names.get(p, p)

    rows = []
    for p, per in m["parents"].items():
        if brand_of.get(p) != brand:
            continue
        w = {str(n): {"buyers": c["buyers"], "same_pct": logic.pct(c["same"], c["buyers"]),
                      "came_from_pct": logic.pct(c["came_from"], c["buyers"]),
                      "went_on_pct": logic.pct(c["went_on"], c["buyers"])}
             for n, c in per.items()}
        flows = {str(n): {k: [{"product": label(q), "customers": v} for q, v in f[k]]
                          for k in ("came_from", "went_on")}
                 for n, f in m["flows"][p].items()}
        b = m["basket"].get(p, {"orders": 0, "multi": 0, "with": []})
        rows.append({
            "parent_asin": p, "product": label(p), "brand": brand_of.get(p, ""),
            "fba_share": min(1.0, fba.get(p, 0) / allc[p]) if allc.get(p) else None,
            "w": w, "flows": flows,
            "basket": {"orders": b["orders"], "multi_pct": logic.pct(b["multi"], b["orders"]),
                       "with": [{"product": label(q), "orders": v} for q, v in b["with"]]},
        })
    rows.sort(key=lambda r: (-max((v["buyers"] for v in r["w"].values()), default=0),
                             r["product"].casefold()))
    total = {str(n): {"buyers": c["buyers"], "repeat_pct": logic.pct(c["repeat"], c["buyers"])}
             for n, c in m["brands"].get(brand, {}).items()}
    windows = {str(n): {"period": [w["period"][0].isoformat(), w["period"][1].isoformat()],
                        "available": w["available"], "reason": w["reason"]}
               for n, w in m["windows"].items()}
    return {"as_of": as_of.isoformat(),
            "history_from": history_from.isoformat() if history_from else None,
            "brand": brand, "brands": sorted(set(brand_of.values())), "windows": windows,
            "total": total, "rows": rows, "last_refresh": last,
            "min_cohort": logic.MIN_COHORT, "fba_partial_below": FBA_PARTIAL_BELOW}
