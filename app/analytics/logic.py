"""Analytics arithmetic. Pure: no database, no Amazon.

Rules carried over from the Profit tab: a percentage is recomputed from sums and never averaged, and a
missing figure is `None` (a dash), never 0 — a product that had not launched has no sales that month,
which is not the same claim as selling nothing.
"""
from __future__ import annotations

from typing import Iterable, Mapping


def month_range(first: str, last: str) -> list[str]:
    """Every `YYYY-MM` from `first` to `last`, inclusive."""
    if not first or not last or first > last:
        return []
    out, (y, m) = [], (int(first[:4]), int(first[5:7]))
    while f"{y:04d}-{m:02d}" <= last:
        out.append(f"{y:04d}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def pct(part, whole):
    if part is None or not whole:
        return None
    return part / whole


def growth(cur, prev):
    """Month-on-month change as a fraction; None when there is nothing to compare against."""
    if cur is None or not prev:
        return None
    return (cur - prev) / prev


def growth_split(prev: Mapping[str, Mapping], cur: Mapping[str, Mapping]) -> dict:
    """Split one product's change in sales into volume, price and mix. They add up EXACTLY.

    `prev` / `cur` are `{pack size asin: {"sales", "units", "kg"}}` (units = units ordered, kg = weight
    ordered or None when the sheet has no weight).

    - price  = for each pack sold in BOTH months: (this month's price - last month's) x this month's
               units. A pack's price is its sales / units, so a price rise on an unchanged pack is
               price and nothing else.
    - volume = the change in kilograms at last month's rupees per kg (units when a weight is
               unknown). A new product, with nothing to compare, is all volume.
    - mix    = what is left: a shift between pack sizes that earn different rupees per kg.
    """
    def total(sizes, key):
        vals = [s.get(key) for s in sizes.values()]
        return sum(v for v in vals if v)

    s0, s1 = total(prev, "sales"), total(cur, "sales")
    delta = s1 - s0
    price = 0.0
    for asin, now in cur.items():
        before = prev.get(asin)
        if not before or not before.get("units") or not now.get("units"):
            continue
        price += (now["sales"] / now["units"] - before["sales"] / before["units"]) * now["units"]

    sold = [s for s in list(prev.values()) + list(cur.values()) if s.get("units")]
    weighed = bool(sold) and all(s.get("kg") is not None for s in sold)
    k0, k1 = total(prev, "kg"), total(cur, "kg")
    u0, u1 = total(prev, "units"), total(cur, "units")
    if not s0:
        volume, price = delta, 0.0          # a launch: nothing to compare a price against
    elif weighed and k0:
        volume = (k1 - k0) * (s0 / k0)
    elif u0:
        volume = (u1 - u0) * (s0 / u0)
    else:
        volume = delta - price
    price, volume = round(price, 2), round(volume, 2)
    return {"delta": round(delta, 2), "volume": volume, "price": price,
            "mix": round(delta - volume - price, 2)}


def add_splits(splits: Iterable[Mapping]) -> dict:
    out = {"delta": 0.0, "volume": 0.0, "price": 0.0, "mix": 0.0}
    for s in splits:
        for k in out:
            out[k] += s[k]
    return {k: round(v, 2) for k, v in out.items()}


def concentration(sales_by_key: Mapping[str, float], n: int = 10):
    """Share of sales held by the top `n` products; None with no sales."""
    values = sorted((v for v in sales_by_key.values() if v and v > 0), reverse=True)
    total = sum(values)
    return pct(sum(values[:n]), total) if total else None


def weighted_rates(rows: Iterable[Mapping]) -> dict:
    """Sessions and page views summed; conversion = units / sessions; Buy Box weighted by page views
    (Amazon's Buy Box % is a share of page views). Never a plain average of the rows' rates."""
    sessions = page_views = units = 0
    bb_weight = bb_views = 0.0
    any_row = False
    for r in rows:
        any_row = True
        sessions += r.get("sessions") or 0
        page_views += r.get("page_views") or 0
        units += r.get("units") or 0
        if r.get("buy_box_pct") is not None and r.get("page_views"):
            bb_weight += r["buy_box_pct"] * r["page_views"]
            bb_views += r["page_views"]
    if not any_row:
        return {"sessions": None, "page_views": None, "conversion": None, "buy_box": None}
    return {"sessions": sessions, "page_views": page_views,
            "conversion": pct(units, sessions),
            "buy_box": (bb_weight / bb_views / 100) if bb_views else None}
