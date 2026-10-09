"""The Repeat customers download: the rows on screen, in the screen's order, as Excel or PDF.

Built on `app.portfolio.export` (one Excel and one PDF writer for both Portfolio sub-tabs), so
percentages are real % cells, a dash is a BLANK cell, and the PDF carries the ₹-capable font.
The browser sends which rows and in what order; every NUMBER comes from the server's own payload.
"""
from __future__ import annotations

from typing import Mapping, Sequence

from app.portfolio.export import Column, Row, Table

#: The screen's window order: longest first.
WINDOWS = ("90", "60", "30")
METRICS = (("buyers", "buyers", "int"), ("same", "same repeat", "pct"),
           ("units", "repeat units", "pct"), ("from", "from other", "pct"))

TOTAL_NOTE = ("Calculated by the app from unique customers, like the screen's total row: a customer "
              "who bought several of these products counts once, so it is NOT the sum or average "
              "of the rows below.")


def columns() -> list[Column]:
    cols = [Column("product", "Product", "text"), Column("category", "Category", "text"),
            Column("fba", "FBA share", "pct")]
    for n in WINDOWS:
        cols += [Column(f"{key}-{n}", f"{n}d {label}", kind) for key, label, kind in METRICS]
    return cols


def _window_values(windows: Mapping, w: Mapping, n: str, *, cross: bool) -> dict:
    """One window's four cells. An unavailable window is blank, never its hidden figures."""
    if not (windows.get(n) or {}).get("available"):
        return {f"{k}-{n}": None for k, _, _ in METRICS}
    c = w.get(n) or {}
    return {f"buyers-{n}": c.get("buyers"),
            f"same-{n}": c.get("same_pct", c.get("repeat_pct")),
            f"units-{n}": c.get("units_pct"),
            f"from-{n}": c.get("came_from_pct") if cross else None}


def build_table(payload: Mapping, ids: Sequence[str] | None, category: str | None) -> Table:
    by_id = {r["parent_asin"]: r for r in payload.get("rows") or []}
    if ids is None:
        chosen = [r for r in payload.get("rows") or []
                  if not category or r.get("category") == category]
    else:
        chosen = [by_id[i] for i in dict.fromkeys(ids) if i in by_id]
    windows = payload.get("windows") or {}
    rows = []
    for r in chosen:
        values = {"product": r.get("product"), "category": r.get("category"),
                  "fba": r.get("fba_share")}
        for n in WINDOWS:
            values.update(_window_values(windows, r.get("w") or {}, n, cross=True))
        rows.append(Row("sku", 0, False, values))
    group = next((c for c in payload.get("categories") or [] if c["category"] == category), None)
    total = group["total"] if group else payload.get("total") or {}
    label = f"{category} — all products" if group else f"{payload.get('brand') or 'Brand'} — all products"
    totals = {}
    for n in WINDOWS:
        totals.update(_window_values(windows, total, n, cross=False))
    return Table(columns(), rows, label, totals)
