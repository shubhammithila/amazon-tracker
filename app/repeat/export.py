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


def build_pdf(table: Table, title: str, subtitle: str, *, partial_below: float = 0.6):
    """A4 landscape laid out like the screen, not a generic grid.

    The screen's two-row heading (90-day / 60-day / 30-day over four columns each), full page
    width, the total row shaded, Repeat units in bold (the figure Brand Analytics reports), an FBA
    share under `partial_below` flagged "partial" as on screen, a key to the four measures, and
    "Page x of y" on every page.
    """
    import io

    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.pdfgen import canvas as rl_canvas
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table as PTable, TableStyle

    from app.portfolio.export import _fonts, display

    regular, bold = _fonts()
    ink, muted = colors.HexColor("#1F2937"), colors.HexColor("#6B7280")
    head_bg, total_bg, zebra = colors.HexColor("#E5E7EB"), colors.HexColor("#DBEAFE"), colors.HexColor("#F9FAFB")

    def esc(s):
        return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    size = 7
    base = ParagraphStyle("b", fontName=regular, fontSize=size, leading=size + 2, textColor=ink)
    right = ParagraphStyle("r", parent=base, alignment=2)
    strong_r = ParagraphStyle("sr", parent=right, fontName=bold)
    head = ParagraphStyle("h", parent=base, fontName=bold, alignment=1, textColor=ink)
    head_l = ParagraphStyle("hl", parent=head, alignment=0)

    cols = table.columns
    grid = [[Paragraph("Product", head_l), Paragraph("Category", head_l),
             Paragraph("FBA share", head)]
            + [x for n in WINDOWS for x in (Paragraph(f"{n}-day", head), "", "", "")],
            ["", "", ""] + [Paragraph(lbl, head) for _ in WINDOWS
                            for lbl in ("Buyers", "Same repeat", "Repeat units", "From other")]]

    def cells(values, *, total=False):
        out = []
        for c in cols:
            v = values.get(c.id) if not (total and c.id == "product") else table.total_label
            text = esc(display(v, c.kind)) if v is not None else ""
            if c.id == "fba" and isinstance(v, (int, float)) and v < partial_below and not total:
                text += ' <font color="#B45309">partial</font>'
            if c.kind == "text":
                style = ParagraphStyle("t", parent=base, fontName=bold) if total else base
            elif c.id.startswith("units-"):
                style = strong_r
            else:
                style = ParagraphStyle("tr", parent=right, fontName=bold) if total else right
            out.append(Paragraph(text, style))
        return out

    grid.append(cells(table.totals, total=True))
    for r in table.rows:
        grid.append(cells(r.values))

    page_w = landscape(A4)[0] - 16 * mm
    fixed = {"category": 58, "fba": 52}
    metric_w = 41
    product_w = page_w - sum(fixed.values()) - metric_w * (len(cols) - 3)
    widths = [product_w, fixed["category"], fixed["fba"]] + [metric_w] * (len(cols) - 3)

    t = PTable(grid, colWidths=widths, repeatRows=2)
    style = [
        ("SPAN", (0, 0), (0, 1)), ("SPAN", (1, 0), (1, 1)), ("SPAN", (2, 0), (2, 1)),
        ("BACKGROUND", (0, 0), (-1, 1), head_bg),
        ("BACKGROUND", (0, 2), (-1, 2), total_bg),
        ("LINEBELOW", (0, 1), (-1, 1), 0.8, colors.HexColor("#9CA3AF")),
        ("LINEBELOW", (0, 2), (-1, 2), 0.8, colors.HexColor("#9CA3AF")),
        ("LINEBELOW", (0, 3), (-1, -1), 0.25, colors.HexColor("#E5E7EB")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ("LEFTPADDING", (0, 0), (-1, -1), 3), ("RIGHTPADDING", (0, 0), (-1, -1), 3),
    ]
    for i, _ in enumerate(WINDOWS):
        first = 3 + i * 4
        style.append(("SPAN", (first, 0), (first + 3, 0)))
        # A rule between windows, so 90 / 60 / 30 read as three blocks as on screen.
        style.append(("LINEBEFORE", (first, 0), (first, -1), 0.8, colors.HexColor("#9CA3AF")))
    for ri in range(3, len(grid)):
        if (ri - 3) % 2:
            style.append(("BACKGROUND", (0, ri), (-1, ri), zebra))
    t.setStyle(TableStyle(style))

    class Numbered(rl_canvas.Canvas):
        """Two passes so every page can say "of N"."""
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self._pages = []

        def showPage(self):
            self._pages.append(dict(self.__dict__))
            self._startPage()

        def save(self):
            total = len(self._pages)
            for state in self._pages:
                self.__dict__.update(state)
                self.setFont(regular, 7)
                self.setFillColor(muted)
                self.drawRightString(landscape(A4)[0] - 8 * mm, 6 * mm,
                                     f"Page {self._pageNumber} of {total}")
                self.drawString(8 * mm, 6 * mm, "Amazon Tracker · Repeat customers")
                super().showPage()
            super().save()

    title_style = ParagraphStyle("title", fontName=bold, fontSize=11, leading=14, textColor=ink)
    sub_style = ParagraphStyle("sub", fontName=regular, fontSize=7.5, leading=10, textColor=muted)
    key = ("<b>Buyers</b> customers who bought in the 30-day cohort period · <b>Same repeat</b> "
           "% of them who bought it again within the window · <b>Repeat units</b> % of the units "
           "bought by those repeat customers, first order included (Brand Analytics' measure) · "
           "<b>From other</b> % who had bought another of our products before · a blank is too few "
           "buyers or too little history · <font color='#B45309'>partial</font> = under "
           f"{partial_below:.0%} of its units ship by FBA, so its repeat reads low.")
    out = io.BytesIO()
    doc = SimpleDocTemplate(out, pagesize=landscape(A4), leftMargin=8 * mm, rightMargin=8 * mm,
                            topMargin=8 * mm, bottomMargin=11 * mm, title=title)
    doc.build([Paragraph(esc(title), title_style), Paragraph(esc(subtitle), sub_style),
               Spacer(1, 2 * mm), t, Spacer(1, 3 * mm), Paragraph(key, sub_style)],
              canvasmaker=Numbered)
    out.seek(0)
    return out
