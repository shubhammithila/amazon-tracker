"""Customer value: LTV, CAC, LTV:CAC, payback and the cohort grid. Pure: lines in, figures out.

The owner's definitions (09 Oct 2026):

* **New customer** = first order of the brand's products since history began (1 Jan 2026), never
  seen before. History is short, so the earliest months over-count "new"; the screen says so.
* **LTV(N)** = what the customers acquired in a cohort PAID us (ex-GST, after promotions, shipping
  excluded) from their first order through day N, per customer. A cohort counts only once ALL of
  its customers have had N days, and only where every line in that span carries its price.
* **First product** = the product that took the most money in the customer's first order: the
  product that brought them in. Each customer has exactly one, so product rows add up.
* **CAC(month)** = ad spend (SP + attributed SB) × that month's FBA unit share ÷ new FBA customers.
  The FBA share charges the customers we can SEE only their part of the ads; Easy Ship buyers carry
  no customer key. Only months whose every day of ad data is held.
* **Payback** = the first day on which a customer's cumulative spend, averaged, reaches the CAC.
  Revenue-based by the owner's choice, so it is about cash back, not profit, until product cost
  (purchase price per kg) is added.
"""
from __future__ import annotations

import calendar
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from statistics import median
from typing import Iterable, Mapping

HORIZONS = (30, 60, 90, 180)
PAYBACK_MAX = 180
GRID_MONTHS = 9
#: Below this many customers an average is noise; the screen shows a dash.
MIN_CUSTOMERS = 20


@dataclass
class _Order:
    day: date
    revenue: dict = field(default_factory=lambda: defaultdict(float))
    units: dict = field(default_factory=lambda: defaultdict(int))
    priced: bool = True


def month_of(d: date) -> str:
    return d.isoformat()[:7]


def month_end(month: str) -> date:
    y, m = int(month[:4]), int(month[5:7])
    return date(y, m, calendar.monthrange(y, m)[1])


def add_months(month: str, k: int) -> str:
    y, m = int(month[:4]), int(month[5:7]) - 1 + k
    return f"{y + m // 12:04d}-{m % 12 + 1:02d}"


def _histories(lines: Iterable[Mapping], in_scope) -> dict[str, list[_Order]]:
    """buyer -> orders (day ascending), keeping only in-scope products. An order's day is its
    earliest line's, as everywhere else on these tabs."""
    orders: dict[str, tuple[str, _Order]] = {}
    for line in lines:
        p = line["parent_asin"]
        if not p or not in_scope(p):
            continue
        key = line["amazon_order_id"]
        if key not in orders:
            orders[key] = (line["buyer_key"], _Order(line["day"]))
        o = orders[key][1]
        o.day = min(o.day, line["day"])
        o.units[p] += int(line.get("units") or 0)
        if line.get("revenue") is None:
            o.priced = False
        else:
            o.revenue[p] += float(line["revenue"])
    by_buyer: dict[str, list[_Order]] = defaultdict(list)
    for buyer, o in orders.values():
        by_buyer[buyer].append(o)
    for hist in by_buyer.values():
        hist.sort(key=lambda o: o.day)
    return by_buyer


def _first_product(order: _Order) -> str:
    return max(order.units, key=lambda p: (order.revenue.get(p, 0.0), order.units[p], p))


def compute(lines, in_scope, as_of: date, priced_from: date | None,
            econ: Mapping, econ_days: set, child_key=lambda child, parent: parent) -> dict:
    """``econ``: {(month, amazon_parent, child): (ad_spend, all_channel_units)}; ``econ_days``: days
    held. ``child_key`` maps an economics child to the row key (the flavour split)."""
    hist = _histories(lines, in_scope)

    # ── who was acquired when, and by what ──
    customers = []          # (cohort month, first product, first day, [(offset_days, revenue, priced)])
    for orders in hist.values():
        first = orders[0]
        events = [((o.day - first.day).days, sum(o.revenue.values()), o.priced) for o in orders]
        customers.append((month_of(first.day), _first_product(first), first.day, events))

    # ── LTV per (scope) for each horizon, over cohorts that have fully matured AND are priced ──
    def cohort_ok(month: str, n: int) -> bool:
        start = date.fromisoformat(month + "-01")
        return (month_end(month) + timedelta(days=n) <= as_of
                and priced_from is not None and start >= priced_from)

    def ltv(group) -> dict:
        out = {}
        for n in HORIZONS:
            eligible = [ev for m, _, _, ev in group if cohort_ok(m, n)]
            if len(eligible) < MIN_CUSTOMERS:
                out[str(n)] = None
                continue
            out[str(n)] = sum(r for ev in eligible for d, r, _ in ev if d <= n) / len(eligible)
        return out

    def payback(group, cac: float | None) -> int | None:
        """First day d with the average cumulative spend (customers whose cohort is d days mature
        and priced) >= CAC. Built as per-cohort day arrays, so it is cheap."""
        if not cac:
            return None
        cohorts: dict[str, list] = defaultdict(list)
        for m, _, _, ev in group:
            cohorts[m].append(ev)
        arrays = {}
        for m, members in cohorts.items():
            arr = [0.0] * (PAYBACK_MAX + 1)
            for ev in members:
                for d, r, _ in ev:
                    if d <= PAYBACK_MAX:
                        arr[d] += r
            run = 0.0
            for i in range(PAYBACK_MAX + 1):
                run += arr[i]
                arr[i] = run
            arrays[m] = (arr, len(members))
        for d in range(PAYBACK_MAX + 1):
            total = count = 0
            for m, (arr, size) in arrays.items():
                if cohort_ok(m, d):
                    total += arr[d]
                    count += size
            if count >= MIN_CUSTOMERS and total / count >= cac:
                return d
        return None

    # ── CAC: monthly, over the months whose every ad-data day is held ──
    full_months = sorted({m for (m, _, _) in econ}
                         if econ else [], key=str)
    full_months = [m for m in full_months
                   if all((date.fromisoformat(m + "-01") + timedelta(days=i)).isoformat() in econ_days
                          for i in range(month_end(m).day))
                   and month_end(m) <= as_of]
    spend = defaultdict(float)          # (month, key) -> ad spend
    all_units = defaultdict(int)        # (month, key) -> all-channel units
    for (m, parent, child), (ad, units) in econ.items():
        k = child_key(child, parent)
        if not in_scope(k):
            continue
        spend[(m, k)] += ad
        all_units[(m, k)] += units
    fba_units = defaultdict(int)
    for line in lines:
        p = line["parent_asin"]
        if p and in_scope(p):
            fba_units[(month_of(line["day"]), p)] += int(line.get("units") or 0)
    new = defaultdict(int)              # (month, first product) -> customers
    for m, fp, _, _ in customers:
        new[(m, fp)] += 1

    def cac_over(keys, months) -> dict:
        """Σ spend × FBA share ÷ Σ new customers, over `months`, for products `keys`."""
        charged = acquired = 0.0
        for m in months:
            for k in keys:
                units_all = all_units.get((m, k), 0)
                share = min(1.0, fba_units.get((m, k), 0) / units_all) if units_all > 0 else 0.0
                charged += spend.get((m, k), 0.0) * share
                acquired += new.get((m, k), 0)
        # `cac_customers`, not `new_customers`: this counts only the CAC months, and spread into a
        # row it must not overwrite the row's all-history count of customers acquired.
        return {"spend_charged": round(charged, 2), "cac_customers": int(acquired),
                "cac": round(charged / acquired, 2) if acquired >= MIN_CUSTOMERS else None}

    products = sorted({fp for _, fp, _, _ in customers})
    every = set(products) | {k for (_, k) in spend}

    rows = []
    for p in products:
        group = [c for c in customers if c[1] == p]
        c = cac_over([p], full_months)
        l = ltv(group)
        rows.append({
            "parent_asin": p, "new_customers": len(group), "ltv": l, **c,
            "ltv_cac": (round(l["90"] / c["cac"], 2) if l["90"] and c["cac"] else None),
            "payback_days": payback(group, c["cac"]),
        })
    total_cac = cac_over(every, full_months)
    total_ltv = ltv(customers)
    monthly = []
    for m in full_months:
        c = cac_over(every, [m])
        monthly.append({"month": m, **c})
    latest = monthly[-1] if monthly else None
    total = {
        "new_customers": len(customers), "ltv": total_ltv, **total_cac,
        "ltv_cac": (round(total_ltv["90"] / total_cac["cac"], 2)
                    if total_ltv["90"] and total_cac["cac"] else None),
        "payback_days": payback(customers, total_cac["cac"]),
        "latest_month": latest,
    }
    return {"rows": rows, "total": total, "cac_months": monthly,
            "grid": grid(customers, as_of, priced_from), "reorder": None}


def grid(customers, as_of: date, priced_from: date | None) -> list[dict]:
    """Cohort heat map: per first-order month, the share of its customers who ordered again in each
    later calendar month, and their cumulative spend per customer. Only COMPLETE months are shown,
    or the current month would always read as a slump."""
    by_month: dict[str, list] = defaultdict(list)
    for m, _, first, ev in customers:
        by_month[m].append((first, ev))
    out = []
    for m in sorted(by_month):
        members = by_month[m]
        cells = []
        for k in range(GRID_MONTHS):
            target = add_months(m, k)
            if month_end(target) > as_of:
                cells.append(None)
                continue
            start = date.fromisoformat(target + "-01")
            end = month_end(target)
            active = spent = 0
            for first, ev in members:
                days = [first + timedelta(days=d) for d, _, _ in ev]
                if k == 0:
                    hit = True
                else:
                    hit = any(start <= day <= end for day in days)
                active += hit
                spent += sum(r for (d, r, _), day in zip(ev, days) if day <= end)
            priced = priced_from is not None and date.fromisoformat(m + "-01") >= priced_from
            cells.append({"active_pct": active / len(members),
                          "revenue_per_customer": (spent / len(members)) if priced else None})
        out.append({"month": m, "customers": len(members), "cells": cells})
    return out


def reorder_days(lines, *, min_gaps: int = MIN_CUSTOMERS) -> dict[str, dict]:
    """Median days between a customer's orders of the SAME product (distinct days), per product.

    For the Repeat tab's "Reorder in" column. Same-day orders are one shopping trip, not a reorder."""
    days: dict[tuple[str, str], set] = defaultdict(set)
    for line in lines:
        if line["parent_asin"]:
            days[(line["parent_asin"], line["buyer_key"])].add(line["day"])
    gaps: dict[str, list[int]] = defaultdict(list)
    for (p, _), ds in days.items():
        ordered = sorted(ds)
        gaps[p] += [(b - a).days for a, b in zip(ordered, ordered[1:])]
    return {p: {"median": median(g) if len(g) >= min_gaps else None, "gaps": len(g)}
            for p, g in gaps.items()}
