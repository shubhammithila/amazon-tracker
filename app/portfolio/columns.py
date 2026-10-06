"""The Portfolio table's columns: which exist, which may be hidden, and the default layout.

The server owns this list and ships it with `GET /portfolio`, the way `group_order` and
`verdict_groups` travel, so the template holds no second copy of which columns are protected — a
copy is a second thing to keep in step, and the server is what refuses an invalid save.

`normalise_column_layout` runs on READ and on WRITE, so a value already stored, or edited by hand,
can never break the table: the `good_rating: 99` lesson from the verdict thresholds.
"""
from __future__ import annotations

#: The key under `users.preferences_json`. Namespaced so another screen can store its own choice
#: later without touching this one.
PREFERENCE_KEY = "portfolio_columns"

#: In DEFAULT order. `locked` = cannot be hidden. `product` is additionally pinned first: it is the
#: row's name and its expand control, so a row without it is not a row.
COLUMNS: list[dict] = [
    {"id": "product",     "label": "Product",  "locked": True},
    {"id": "verdict",     "label": "Verdict",  "locked": False},
    # **ex-GST**: Amazon's `orderedProductSales` excludes the 5% GST the order price includes —
    # measured, app sales = order price / 1.05 to within 0.2% for 5 Sep - 4 Oct — so Seller
    # Central's Business Report reads ~5% higher. The label says so rather than leaving a gap to explain.
    {"id": "sales",       "label": "Sales (ex-GST)", "locked": True},
    # **Refunds, Amazon fees and Net (₹) read left to right as Amazon's own sum** — asked for as
    # "100 - 40 (Amazon fees) - 30 (ads) = net 30". Sales - Refunds - Amazon fees - Ad spend = Net,
    # exactly, on every row; refunds are a fourth term the example left out, and without them the
    # row would not add up on screen. Fees exclude ads, which keep their own column.
    {"id": "refunded",    "label": "Refunds",  "locked": False},
    {"id": "fees_total",  "label": "Amazon fees", "locked": False},
    {"id": "ad_spend",    "label": "Ad spend", "locked": True},
    {"id": "net",         "label": "Net (₹)",  "locked": False},
    {"id": "tacos",       "label": "TACOS",    "locked": False},
    {"id": "acos",        "label": "ACOS",     "locked": False},
    {"id": "net_pct",     "label": "Net %",    "locked": False},
    # **Two unit columns, because one could never match Seller Central.** "Units" used to show
    # units AFTER refunds (`netUnitsSold`), which the Business Report never shows: Bengali Posta
    # read 106 + 84 against the report's 113 + 86, the 7 + 2 refunded. `units_ordered` is the
    # Business Report's own figure; net units stay, labelled, because verdicts are decided on them.
    {"id": "units_ordered", "label": "Units ordered", "locked": True},
    {"id": "units",       "label": "Net units", "locked": True},
    {"id": "weight_ordered_kg", "label": "Weight ordered", "locked": True},
    {"id": "weight_kg",   "label": "Net weight", "locked": True},
    {"id": "returns_pct", "label": "Returns",  "locked": False},
    {"id": "rating",      "label": "Rating",   "locked": False},
    {"id": "decision",    "label": "Decision", "locked": False},
]

PINNED_FIRST = "product"
_MOVABLE = [c["id"] for c in COLUMNS if c["id"] != PINNED_FIRST]
_LOCKED = {c["id"] for c in COLUMNS if c["locked"]}

#: Today's screen: everything in default order, Returns hidden.
DEFAULT_LAYOUT: dict = {"order": list(_MOVABLE), "hidden": ["returns_pct"]}


def _default() -> dict:
    return {"order": list(DEFAULT_LAYOUT["order"]), "hidden": list(DEFAULT_LAYOUT["hidden"])}


def normalise_column_layout(saved: object) -> dict:
    """Any saved value -> a valid ``{"order": [...], "hidden": [...]}``. Never raises.

    1. malformed -> the default;
    2. unknown ids dropped (a renamed or removed column);
    3. duplicates dropped, first occurrence kept;
    4. **a known column missing from `order` is inserted after its nearest default-order
       predecessor that IS present** (or at the front if none is), so a column added later APPEARS
       for a user who customised, instead of silently never showing;
    5. `product` removed from `order` — it is pinned first and never stored;
    6. protected ids removed from `hidden`.
    """
    if not isinstance(saved, dict):
        return _default()
    order = saved.get("order")
    hidden = saved.get("hidden", [])
    if not isinstance(order, list) or not isinstance(hidden, list):
        return _default()
    if not all(isinstance(x, str) for x in order) or not all(isinstance(x, str) for x in hidden):
        return _default()

    known = set(_MOVABLE)
    clean: list[str] = []
    for col in order:
        if col in known and col not in clean:
            clean.append(col)

    for index, col in enumerate(_MOVABLE):
        if col in clean:
            continue
        predecessors = [p for p in _MOVABLE[:index] if p in clean]
        at = clean.index(predecessors[-1]) + 1 if predecessors else 0
        clean.insert(at, col)

    hide: list[str] = []
    for col in hidden:
        if col in known and col not in _LOCKED and col not in hide:
            hide.append(col)

    return {"order": clean, "hidden": hide}
