"""A naive day-by-day re-statement of the definitions, compared with logic.metrics on random data.

Hand-built cases prove the definitions; this proves the real code agrees with a deliberately simple
one on shapes nobody thought to build by hand. It shares NO code with logic.metrics.
"""
import random
from datetime import date, timedelta

import pytest

from app.repeat import logic

pytestmark = pytest.mark.regression
AS_OF = date(2026, 10, 1)
PRODUCTS = ["CS", "JS", "RC", "HF"]
BRAND = {"CS": "M", "JS": "M", "RC": "M", "HF": "H"}


def _orders_by_day(lines):
    """(buyer, day) -> {order_id: set(products)}."""
    out = {}
    for l in lines:
        out.setdefault((l["buyer_key"], l["day"]), {}).setdefault(
            l["amazon_order_id"], set()).add(l["parent_asin"])
    return out


def _units(lines):
    """(buyer, day, product) -> units, summed over every line of that day."""
    out = {}
    for l in lines:
        k = (l["buyer_key"], l["day"], l["parent_asin"])
        out[k] = out.get(k, 0) + l["units"]
    return out


def _span_units(units, buyer, anchor, n, products):
    """Units of `products` from the anchor day through anchor + n, day by day."""
    return sum(units.get((buyer, anchor + timedelta(days=k), q), 0)
               for k in range(0, n + 1) for q in products)


def naive(lines, n):
    start, end = AS_OF - timedelta(days=n + 29), AS_OF - timedelta(days=n)
    bought = _orders_by_day(lines)
    buyers = {l["buyer_key"] for l in lines}
    units = _units(lines)
    res = {}
    for p in PRODUCTS:
        b = s = c = w = u = ru = 0
        for buyer in buyers:
            d, anchor = start, None
            while d <= end and anchor is None:
                if any(p in ps for ps in bought.get((buyer, d), {}).values()):
                    anchor = d
                d += timedelta(days=1)
            if anchor is None:
                continue
            b += 1
            later = [ps for k in range(1, n + 1)
                     for ps in bought.get((buyer, anchor + timedelta(days=k)), {}).values()]
            earlier = [ps for k in range(1, n + 1)
                       for ps in bought.get((buyer, anchor - timedelta(days=k)), {}).values()]
            got = _span_units(units, buyer, anchor, n, [p])
            u += got
            if any(p in ps for ps in later):
                s += 1
                ru += got
            w += any(ps - {p} for ps in later)
            c += any(ps - {p} for ps in earlier)
        if b:
            res[p] = {"buyers": b, "same": s, "came_from": c, "went_on": w,
                      "units": u, "repeat_units": ru}
    return res


def naive_brand(lines, n, brand):
    start, end = AS_OF - timedelta(days=n + 29), AS_OF - timedelta(days=n)
    bought = _orders_by_day(lines)
    buyers = {l["buyer_key"] for l in lines}
    mine = {p for p, b in BRAND.items() if b == brand}
    units = _units(lines)
    total = rep = u = ru = 0
    for buyer in buyers:
        d, anchor = start, None
        while d <= end and anchor is None:
            if any(ps & mine for ps in bought.get((buyer, d), {}).values()):
                anchor = d
            d += timedelta(days=1)
        if anchor is None:
            continue
        total += 1
        got = _span_units(units, buyer, anchor, n, mine)
        u += got
        if any(ps & mine for k in range(1, n + 1)
               for ps in bought.get((buyer, anchor + timedelta(days=k)), {}).values()):
            rep += 1
            ru += got
    return {"buyers": total, "repeat": rep, "units": u, "repeat_units": ru} if total else None


def _random_lines(seed):
    rnd = random.Random(seed)
    lines = []
    for i in range(rnd.randint(50, 400)):
        buyer = f"b{rnd.randint(0, 60)}"
        day = AS_OF - timedelta(days=rnd.randint(0, 260))
        for p in rnd.sample(PRODUCTS, rnd.choice([1, 1, 1, 2])):
            lines.append({"amazon_order_id": f"o{i}", "buyer_key": buyer, "day": day,
                          "parent_asin": p, "units": rnd.choice([1, 1, 2, 3])})
    return lines


@pytest.mark.parametrize("seed", range(40))
def test_metrics_agree_with_the_naive_restatement(seed):
    lines = _random_lines(seed)
    m = logic.metrics(logic.build_orders(lines), BRAND, AS_OF, date(2025, 1, 1))
    for n in logic.WINDOWS:
        got = {p: w[n] for p, w in m["parents"].items() if n in w}
        assert got == naive(lines, n), f"seed {seed}, window {n}"
        for brand in ("M", "H"):
            assert m["brands"].get(brand, {}).get(n) == naive_brand(lines, n, brand), \
                f"seed {seed}, window {n}, brand {brand}"
