"""Repeat-customer metrics. Pure: order lines in, counts out. Every figure on the tab comes from here.

For window N (30, 60, 90) the cohort period is the 30 days ending `as_of - N`, so every customer in
it has had the full N days to come back and the three windows are comparable. A customer joins
product X's cohort on their FIRST purchase of X in that period (the anchor). Then:

* **same**      an order containing X on a LATER day, within N days of the anchor
* **went_on**   an order containing a DIFFERENT product, later day, within N days after
* **came_from** an order containing a DIFFERENT product, earlier day, within N days before

A second order on the same day is not a repeat, and two products in one order are a BASKET, not a
cross flow. For a brand, the anchor is the first purchase of ANY of its products in the period and
**repeat** is any later-day order of one of its products within N days — counted per UNIQUE
customer, so the brand total is not the sum of its rows.

A window is unavailable when stored history does not reach `period_start - N`, because came_from
looks back N days. Percentages come from counts (`pct`), and are None below MIN_COHORT buyers.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Iterable, Mapping, Sequence

from app.portfolio.logic import family_label

WINDOWS = (30, 60, 90)
PERIOD_DAYS = 30
#: Below this many buyers a percentage is noise; the screen shows a dash and the count.
MIN_COHORT = 20
FLOW_TOP = 8
BASKET_DAYS = 90


@dataclass(frozen=True)
class Order:
    order_id: str
    buyer: str
    day: date
    parents: frozenset


def build_orders(lines) -> list[Order]:
    acc: dict[str, list] = {}
    for line in lines:
        if not line.get("parent_asin"):
            continue
        o = acc.setdefault(line["amazon_order_id"], [line["buyer_key"], line["day"], set()])
        o[2].add(line["parent_asin"])
        o[1] = min(o[1], line["day"])
    return [Order(k, b, d, frozenset(p)) for k, (b, d, p) in acc.items()]


def period(n: int, as_of: date) -> tuple[date, date]:
    end = as_of - timedelta(days=n)
    return end - timedelta(days=PERIOD_DAYS - 1), end


def window_status(n: int, as_of: date, history_from: date | None) -> tuple[bool, str | None]:
    need = period(n, as_of)[0] - timedelta(days=n)
    if history_from is None or history_from > need:
        return False, f"needs order history from {need.isoformat()}"
    return True, None


def pct(part: int, whole: int) -> float | None:
    if whole < MIN_COHORT:
        return None
    return part / whole


def _top(counter: Counter) -> list[tuple[str, int]]:
    return sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))[:FLOW_TOP]


def metrics(orders: Sequence[Order], brand_of: Mapping[str, str], as_of: date,
            history_from: date | None) -> dict:
    by_buyer: dict[str, list[Order]] = defaultdict(list)
    for o in orders:
        by_buyer[o.buyer].append(o)
    for history in by_buyer.values():
        history.sort(key=lambda o: (o.day, o.order_id))
    out = {"windows": {}, "parents": defaultdict(dict), "brands": defaultdict(dict),
           "flows": defaultdict(dict)}
    for n in WINDOWS:
        start, end = period(n, as_of)
        ok, reason = window_status(n, as_of, history_from)
        out["windows"][n] = {"period": (start, end), "available": ok, "reason": reason}
        if not ok:
            continue
        span = timedelta(days=n)
        counts = defaultdict(lambda: {"buyers": 0, "same": 0, "came_from": 0, "went_on": 0})
        came, went = defaultdict(Counter), defaultdict(Counter)
        brand_counts = defaultdict(lambda: {"buyers": 0, "repeat": 0})
        for history in by_buyer.values():
            in_period = [o for o in history if start <= o.day <= end]
            if not in_period:
                continue
            anchors: dict[str, date] = {}
            for o in in_period:
                for p in o.parents:
                    anchors.setdefault(p, o.day)
            for p, anchor in anchors.items():
                c = counts[p]
                c["buyers"] += 1
                after = [o for o in history if anchor < o.day <= anchor + span]
                before = [o for o in history if anchor - span <= o.day < anchor]
                if any(p in o.parents for o in after):
                    c["same"] += 1
                dest = {q for o in after for q in o.parents if q != p}
                src = {q for o in before for q in o.parents if q != p}
                if dest:
                    c["went_on"] += 1
                    went[p].update(dest)
                if src:
                    c["came_from"] += 1
                    came[p].update(src)
            brand_anchor: dict[str, date] = {}
            for o in in_period:
                for p in o.parents:
                    if brand_of.get(p):
                        brand_anchor.setdefault(brand_of[p], o.day)
            for b, anchor in brand_anchor.items():
                bc = brand_counts[b]
                bc["buyers"] += 1
                if any(anchor < o.day <= anchor + span
                       and any(brand_of.get(q) == b for q in o.parents) for o in history):
                    bc["repeat"] += 1
        for p, c in counts.items():
            out["parents"][p][n] = dict(c)
            out["flows"][p][n] = {"came_from": _top(came[p]), "went_on": _top(went[p])}
        for b, bc in brand_counts.items():
            out["brands"][b][n] = dict(bc)
    first = as_of - timedelta(days=BASKET_DAYS - 1)
    basket = defaultdict(lambda: {"orders": 0, "multi": 0, "with": Counter()})
    for o in orders:
        if not first <= o.day <= as_of:
            continue
        for p in o.parents:
            k = basket[p]
            k["orders"] += 1
            others = o.parents - {p}
            if others:
                k["multi"] += 1
                k["with"].update(others)
    out["basket"] = {p: {"orders": k["orders"], "multi": k["multi"], "with": _top(k["with"])}
                     for p, k in basket.items()}
    return out


def covered_from(runs: Iterable[tuple[str, str]], until: date) -> date | None:
    """Start of the CONTIGUOUS stored history that reaches `until`, from the done fetch windows."""
    merged: list[list[date]] = []
    for a, b in sorted((date.fromisoformat(a), date.fromisoformat(b)) for a, b in runs):
        if merged and a <= merged[-1][1] + timedelta(days=1):
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    for a, b in merged:
        if a <= until <= b:
            return a
    return None


def name_parents(children: Mapping[str, Sequence[str]]) -> dict[str, str]:
    """The Profit view's naming rule, so a product reads the same on both sub-tabs.

    Children arrive BIGGEST SELLER FIRST. One flavour keeps its name; several get `family_label`;
    only a DERIVED name that collides with another gets "(N flavours)".
    """
    names, derived = {}, {}
    for parent, child_names in children.items():
        distinct = list(dict.fromkeys(n for n in child_names if n))
        flavours = list(dict.fromkeys(n.casefold() for n in distinct))
        if len(flavours) > 1:
            names[parent], derived[parent] = family_label(distinct), len(flavours)
        else:
            names[parent] = distinct[0] if distinct else parent
    taken = Counter(names.values())
    for parent, count in derived.items():
        if taken[names[parent]] > 1:
            names[parent] = f"{names[parent]} ({count} flavours)"
    return names
