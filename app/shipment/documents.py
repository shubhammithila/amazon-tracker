"""The shipment downloads. Each returns an in-memory io.BytesIO.

Four working documents, three of which share one column layout:

  plan       xlsx + pdf   what to pack this week, for the owner
  packed     xlsx + pdf   what was boxed, over a date or a range
  remaining  xlsx + pdf   the morning clipboard sheet, for the packer
  shipment file  xlsx     the merchant-SKU + quantity upload for Amazon

The first three go through ``build_simple_xlsx`` / ``build_simple_pdf``, differing
only in the quantity column.

> Four earlier builders lived here — ``build_packing_plan_xlsx``,
> ``build_packing_plan_pdf``, ``build_packed_xlsx`` and ``build_remaining_pdf`` —
> each with its own column list. The shared layout replaced them on every route,
> and then their own tests kept them alive for a whole step. That is worse than
> plain dead code: twelve tests went on asserting column content for sheets nobody
> could download, so this file read as though it described what the owner gets.
> Deleted, and the tests with them.

**Every builder takes rows that are already sorted and renders them in the order
given.** None of them sorts. Row order comes from
``repository.load_plan_items()`` and nowhere else, which is what stops a download
from disagreeing with the screen the owner was just looking at. There is a test
that reads a generated xlsx back and asserts its row order equals
``logic.sort_items``; if you add a ``sorted()`` call in here, that is what fails.

Nothing here touches the database or FastAPI — the router loads and orders the
rows, these functions only format. That keeps them directly unit-testable and
means an ordering bug can only have one home.
"""
import io
import logging
from datetime import date

from app.shipment import logic

logger = logging.getLogger(__name__)

#: Matches the invoice PDFs (app/invoice/generator.py) so the shipment downloads
#: do not look like they came from a different product.
HEADER_RGB = (0.2, 0.3, 0.3)

#: openpyxl wants "RRGGBB"; reportlab wants floats. Derived from HEADER_RGB so
#: the Excel and PDF documents cannot drift apart in appearance.
HEADER_HEX = "".join(f"{int(round(c * 255)):02X}" for c in HEADER_RGB)


def _weight_label(weight) -> str:
    """Pack size as the warehouse says it — see logic.weight_label.

    Kept as a thin alias because this module calls it in several places and the
    name reads better in a row-building expression. The RULE lives in logic.py with
    the other shared rules: grams under 1 kg, kilos above, and one implementation
    so the printed sheet cannot disagree with the screen.
    """
    return logic.weight_label(weight)


# ─── Excel ───────────────────────────────────────────────────────────────────

def _autosize(worksheet, widths: list[int]) -> None:
    from openpyxl.utils import get_column_letter

    for index, width in enumerate(widths, start=1):
        worksheet.column_dimensions[get_column_letter(index)].width = width


def _write_sheet(worksheet, headers: list[str], rows: list[list], widths: list[int]) -> None:
    """Header row + data rows, with the header frozen and styled.

    Freezing matters in practice: the packed sheet grows a column per packing day,
    so by the end of a week the owner is scrolling and would otherwise lose track
    of which column is which date.
    """
    from openpyxl.styles import Alignment, Font, PatternFill

    worksheet.append(headers)
    fill = PatternFill("solid", fgColor=HEADER_HEX)
    for cell in worksheet[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = fill
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

    for row in rows:
        worksheet.append(row)

    worksheet.freeze_panes = "A2"
    _autosize(worksheet, widths)


def build_shipment_file_xlsx(
    items: list[dict], mode: str = "remaining", days: list[dict] | None = None
) -> io.BytesIO:
    """The Amazon upload sheet: merchant SKU + quantity.

    ``mode`` picks the quantity column:

      remaining  what is still to be packed (planning a future shipment)
      all        the full planned quantity
      verified   only units on days the owner has verified

    Rows whose quantity is 0 are omitted — Amazon has nothing to do with them and
    they only make the file harder to check.

    A row with no merchant SKU is written with a blank SKU rather than silently
    falling back to the ASIN. Amazon's upload keys on the merchant SKU, so an
    ASIN there is rejected on their side; a visible blank is a problem the owner
    can fix before uploading, whereas a plausible-looking ASIN is one they find
    out about from Amazon.
    """
    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Shipment"

    verified = logic.verified_units_by_asin(days or [])

    rows = []
    missing_sku = 0
    for item in items:
        asin = item.get("asin", "")
        planned = int(item.get("shipment_plan") or 0)
        if mode == "all":
            quantity = planned
        elif mode == "verified":
            quantity = int(verified.get(asin, 0))
        else:
            quantity = int(item.get("remaining") or 0)

        if quantity <= 0:
            continue
        sku = item.get("fba_sku") or ""
        if not sku:
            missing_sku += 1
        rows.append([sku, asin, item.get("item", ""), _weight_label(item.get("weight")), quantity])

    if missing_sku:
        # Surfaced rather than swallowed: this used to fail invisibly.
        logger.warning(
            "shipment file: %d row(s) have no merchant SKU and will be rejected by Amazon",
            missing_sku,
        )

    _write_sheet(
        sheet,
        ["Merchant SKU", "ASIN", "Product", "Weight", "Quantity"],
        rows,
        [22, 14, 30, 9, 11],
    )

    buffer = io.BytesIO()
    workbook.save(buffer)
    buffer.seek(0)
    return buffer


# ─── The three working documents ─────────────────────────────────────────────
#
# One column layout, requested explicitly:
#
#     S · M · B · Brand · ASIN · SKU · Product · Pack Size · <quantity>
#
# The quantity column is the only thing that differs, because each sheet answers a
# different question: the plan and the remaining sheet say what is still TO PACK,
# while the packed sheet says what WAS packed.
#
# All three drop rows with nothing to do. That is the point of them: a 205-row
# sheet where 88 rows read 0 is a sheet nobody checks, and the owner asked for
# "only the final rows".
#
# Both formats are generated from the SAME rows list by the same two functions
# below, so the Excel and the PDF of one document cannot disagree.
#
# **Visual hierarchy is a correctness feature on these, not decoration.** They are
# read on a clipboard, at arm's length, by someone holding a carton. Three cells
# decide what he does — product, pack size, quantity — and the other five only
# matter when something has gone wrong and a line has to be identified. So those
# three are set large and bold and the identifiers are set small and grey. Printing
# all eight at one weight is what produced "which of these numbers am I packing".

#: The shared header, minus the quantity column.
#:
#: ``Pack Size`` is its own column rather than being glued onto the product name.
#: They were combined to save width on a portrait page, and it was wrong: the eye
#: cannot scan a column of sizes that are buried at the end of names of different
#: lengths, which is exactly the scan the packer makes when he has all sizes of one
#: product in front of him. Separated, the sizes line up.
#:
#: The first seven are unchanged and in the order the owner asked for; a test pins
#: that prefix, so ``Pack Size`` is appended rather than inserted.
IDENTITY_HEADERS = ["S", "M", "B", "Brand", "ASIN", "Merchant SKU", "Product", "Pack Size"]
IDENTITY_WIDTHS = [4, 4, 4, 7, 15, 24, 32, 11]

#: Column headings whose cells are identifiers: needed to resolve a query, never
#: read while packing. Rendered small and grey in both formats.
QUIET_HEADERS = frozenset({"ASIN", "Merchant SKU"})

#: The headings that carry the actual instruction. Rendered large and bold.
LOUD_HEADERS = frozenset({"Product", "Pack Size"})


def _identity_cells(item: dict) -> list:
    """The eight columns every one of the three documents starts with.

    S/M/B are rendered as a tick or blank rather than TRUE/FALSE: they are carton
    sizes being read off a page, and "S Y" scans in a way "S TRUE" does not.
    """
    return [
        "Y" if item.get("s") else "",
        "Y" if item.get("m") else "",
        "Y" if item.get("b") else "",
        item.get("brand", ""),
        item.get("asin", ""),
        item.get("fba_sku", ""),
        item.get("item", ""),
        _weight_label(item.get("weight")),
    ]


def _rows_with_quantity(items: list[dict], quantity_key: str) -> list[list]:
    """Identity columns + one quantity, keeping only rows with something to do."""
    rows = []
    for item in items:
        quantity = int(item.get(quantity_key) or 0)
        if quantity <= 0:
            continue
        rows.append(_identity_cells(item) + [quantity])
    return rows


#: Where the "TOTAL · N rows" label is written on the totals line. The Product
#: column, because it is the widest and the one the eye is already running down.
_TOTALS_LABEL_COLUMN = IDENTITY_HEADERS.index("Product")


def _totals_row(headers: list[str], rows: list[list]) -> list:
    """A totals line under the data, summing every quantity column present.

    Driven by the header count rather than a fixed position, so the packed sheet's
    two quantity columns (units and cartons) total correctly without a second
    implementation. The label position is derived from IDENTITY_HEADERS too — it
    was a hand-counted run of blanks, which silently shifted when Pack Size was
    added and put the label under a quantity heading.
    """
    totals: list = [""] * len(IDENTITY_HEADERS)
    totals[_TOTALS_LABEL_COLUMN] = f"TOTAL · {len(rows)} rows"
    for offset in range(len(headers) - len(IDENTITY_HEADERS)):
        column = len(IDENTITY_HEADERS) + offset
        totals.append(sum(int(row[column] or 0) for row in rows))
    return totals


def build_simple_xlsx(
    title: str, subtitle: str, headers: list[str], rows: list[list], widths: list[int]
) -> io.BytesIO:
    """A one-sheet workbook in the shared layout, with a totals row.

    The same emphasis as the PDF: quantity and product bold, ASIN and SKU small and
    grey. Deliberately mirrored rather than left plain, because the owner reads the
    Excel of the same document the packer holds on paper, and two versions that look
    unrelated get treated as two different reports.
    """
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = title[:31]  # Excel's limit; a longer name makes the file invalid.

    body = list(rows) + ([_totals_row(headers, rows)] if rows else [])
    _write_sheet(sheet, headers, body, widths)

    quiet = Font(size=9, color="FF6B7280")
    loud = Font(size=12, bold=True)
    quantity_font = Font(size=12, bold=True)
    for index, heading in enumerate(headers, start=1):
        for row in sheet.iter_rows(min_row=2, min_col=index, max_col=index):
            cell = row[0]
            if heading in QUIET_HEADERS:
                cell.font = quiet
            elif heading in LOUD_HEADERS:
                cell.font = loud
            elif index > len(IDENTITY_HEADERS):
                cell.font = quantity_font
                cell.alignment = Alignment(horizontal="right")

    # The totals line, bold across the whole width so it cannot be misread as data.
    if rows:
        for cell in sheet[sheet.max_row]:
            cell.font = Font(size=11, bold=True)

    sheet.row_dimensions[1].height = 22
    buffer = io.BytesIO()
    workbook.save(buffer)
    buffer.seek(0)
    return buffer


def build_simple_pdf(
    title: str, subtitle: str, headers: list[str], rows: list[list]
) -> io.BytesIO:
    """The same rows as a portrait A4 PDF — a clipboard page, not a wide report.

    **Every cell is a Paragraph, not a string, and that is the fix for a real bug
    rather than a styling choice.** reportlab does not wrap a plain string in a
    table cell: it draws it at full width and lets it run straight over the
    gridline into the next column. Merchant SKUs here are up to 24 characters
    ("Beetroot_Sattu_500g FBA"), so the SKU column was printing on top of the
    product column on most rows of a real 117-row plan. A Paragraph wraps, and the
    row grows to fit instead.

    The typographic hierarchy is set here rather than in the table style because it
    is per-column and reportlab's TableStyle can only set a font over a rectangle —
    which is fine, but the wrapping already requires per-cell objects, so the
    emphasis rides along for free and stays in one place.
    """
    from reportlab.lib.units import mm
    from reportlab.platypus import Table

    buffer = io.BytesIO()
    doc, elements = _pdf_document(buffer, title, subtitle, landscape_mode=False)

    body = list(rows) + ([_totals_row(headers, rows)] if rows else [])
    if not body:
        body = [["—"] * len(headers)]
    totals_index = len(body) - 1 if rows else None

    data = [[_head_cell(h) for h in headers]]
    for position, row in enumerate(body):
        emphasise = position == totals_index
        data.append([
            _body_cell(row[column], headers[column], column, len(headers), emphasise)
            for column in range(len(headers))
        ])

    table = Table(data, colWidths=_pdf_column_widths(headers, body), repeatRows=1)
    table.setStyle(
        _pdf_table_style(
            len(headers),
            # +1 for the header row, which is data[0].
            totals_row=totals_index + 1 if totals_index is not None else None,
        )
    )
    elements.append(table)
    doc.build(elements, onFirstPage=_page_footer, onLaterPages=_page_footer)
    buffer.seek(0)
    return buffer


#: A4 portrait (210 mm) minus the 10 mm margins each side.
_PAGE_WIDTH_MM = 190

#: LEFTPADDING + RIGHTPADDING from the table style, in mm. A column must be this
#: much wider than its text or the text wraps — the trap that wrapped every ASIN
#: when the widths were first measured.
_PADDING_MM = 8 / 72 * 25.4  # 4pt + 4pt ≈ 2.8 mm

#: Slack on top of the measured content width. An EXACT fit wraps: reportlab breaks
#: when the text is >= the available width, and float arithmetic makes "equal" a
#: coin toss. Observed as "4560" wrapping to "456" / "0" in a totals row whose
#: column had been sized to precisely 8.6303 mm for 8.6303 mm of digits.
_SLACK_MM = 0.8


#: Columns that must never wrap, with the ceiling each may claim (mm). An ASIN or a
#: pack size broken across two lines is unreadable rather than merely ugly — "B0GW3
#: 88QP6" cannot be typed into a search box, and "1.75" over "kg" reads as two
#: facts. Every one of these has bounded content, so a ceiling is safe.
_NO_WRAP_CEILING_MM = {"S": 8, "M": 8, "B": 8, "Brand": 14, "ASIN": 24, "Pack Size": 20}

#: Columns allowed to wrap when their content is genuinely long, and the width past
#: which they will. The Merchant SKU's longest real value is 68 mm — giving it that
#: would take a third of the page for a string nobody reads while packing.
_WRAP_CEILING_MM = {"Merchant SKU": 42}

#: Quantity columns. Bounded by construction (an integer), so a ceiling is safe.
_QUANTITY_CEILING_MM = 22

#: Product never gets less than this, however many quantity columns are added.
_PRODUCT_FLOOR_MM = 34


def _pdf_column_widths(headers: list[str], rows: list[list]) -> list:
    """Column widths in points, summing to exactly the printable width.

    **Measured from the rows being rendered, not from constants.** Two earlier passes
    hardcoded millimetres — the first by eye, which gave Product 62 mm when three
    quarters of the names need 32, and the second from percentiles of the catalogue,
    which still wrapped every ASIN because it forgot that padding eats 2.8 mm of a
    column. Both were the same mistake: a number written down here has to be right
    about data it cannot see, and it silently degrades when the data changes.

    So each column asks for what its own longest value actually needs at its own font
    size, capped so one freak value cannot take the page, and **Product absorbs the
    remainder**. Product is the flexible one because its content has no natural bound
    and because it is the least harmful to wrap: a long name over two lines is still
    completely readable, where a split ASIN is not.

    The consequence worth knowing: the packed sheet's extra quantity column narrows
    Product rather than squeezing the digits or clipping the SKU.
    """
    from reportlab.lib.units import mm
    from reportlab.pdfbase.pdfmetrics import stringWidth

    def content_mm(column: int, heading: str) -> float:
        """The widest cell in this column, plus its header, at the right font."""
        style = _body_cell_style_name(heading, column)
        font, size = _STYLE_FONTS[style]
        widest = max(
            (stringWidth(str(row[column] or ""), font, size) for row in rows),
            default=0.0,
        )
        # The header is bold 8.5pt and can easily be the widest thing in a narrow
        # column ("Cartons" beats any 3-digit carton count).
        widest = max(widest, stringWidth(heading, "Helvetica-Bold", 8.5))
        return widest / mm + _PADDING_MM + _SLACK_MM

    widths_mm: list[float | None] = []
    for column, heading in enumerate(headers):
        if heading == "Product":
            widths_mm.append(None)
            continue
        needed = content_mm(column, heading)
        if heading in _NO_WRAP_CEILING_MM:
            widths_mm.append(min(needed, _NO_WRAP_CEILING_MM[heading]))
        elif heading in _WRAP_CEILING_MM:
            widths_mm.append(min(needed, _WRAP_CEILING_MM[heading]))
        else:
            widths_mm.append(min(needed, _QUANTITY_CEILING_MM))

    spare = _PAGE_WIDTH_MM - sum(w for w in widths_mm if w is not None)
    return [
        (max(_PRODUCT_FLOOR_MM, spare) if w is None else w) * mm for w in widths_mm
    ]


def _paragraph_styles():
    """Cached paragraph styles for the table cells.

    Built once per process rather than per cell: a 205-row plan with nine columns is
    1,845 cells, and constructing a ParagraphStyle for each one is measurable.
    """
    global _STYLES
    if _STYLES is not None:
        return _STYLES

    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER, TA_RIGHT
    from reportlab.lib.styles import ParagraphStyle

    base = ParagraphStyle(
        "Cell", fontName="Helvetica", fontSize=9, leading=11, textColor=colors.black
    )
    _STYLES = {
        # Identifiers: present for lookups, deliberately recessive. 7.5pt grey is
        # still legible on paper at reading distance — this is de-emphasis, not
        # fine print.
        "quiet": ParagraphStyle(
            "Quiet", parent=base, fontSize=7.5, leading=9,
            textColor=colors.Color(0.42, 0.45, 0.50),
        ),
        # What the packer is actually reading.
        "loud": ParagraphStyle(
            "Loud", parent=base, fontName="Helvetica-Bold", fontSize=10.5, leading=12.5
        ),
        "quantity": ParagraphStyle(
            "Qty", parent=base, fontName="Helvetica-Bold", fontSize=11, leading=13,
            alignment=TA_RIGHT,
        ),
        "flag": ParagraphStyle("Flag", parent=base, alignment=TA_CENTER),
        "plain": base,
        "totals": ParagraphStyle(
            "Totals", parent=base, fontName="Helvetica-Bold", fontSize=10, leading=12
        ),
        "totals_qty": ParagraphStyle(
            "TotalsQty", parent=base, fontName="Helvetica-Bold", fontSize=11,
            leading=13, alignment=TA_RIGHT,
        ),
        "head": ParagraphStyle(
            "Head", parent=base, fontName="Helvetica-Bold", fontSize=8.5, leading=10,
            textColor=colors.white,
        ),
    }
    return _STYLES


_STYLES = None


def _escape(value) -> str:
    """Text for a Paragraph. Product names come from an uploaded CSV.

    Paragraph parses its input as mini-HTML, so an ampersand in a product name
    ("Salt & Pepper") raises a parse error and takes the whole download down with a
    500. Found the same way the template escaping was.
    """
    return (
        str("" if value is None else value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _head_cell(heading: str):
    from reportlab.platypus import Paragraph

    return Paragraph(_escape(heading), _paragraph_styles()["head"])


#: Font and size per style name, so _pdf_column_widths can measure a cell without
#: instantiating its Paragraph. Kept beside _body_cell_style_name because the two
#: must agree: a style added to one and not the other measures at the wrong size and
#: the column silently wraps.
_STYLE_FONTS = {
    "quiet": ("Helvetica", 7.5),
    "loud": ("Helvetica-Bold", 10.5),
    "quantity": ("Helvetica-Bold", 11),
    "flag": ("Helvetica", 9),
    "plain": ("Helvetica", 9),
    "totals": ("Helvetica-Bold", 10),
    "totals_qty": ("Helvetica-Bold", 11),
}


def _body_cell_style_name(heading: str, column: int, totals: bool = False) -> str:
    """Which style a cell gets. The single rule, read by both renderer and measurer.

    Order matters: the quantity test comes before the heading tests so a quantity
    column named "Units" is right-aligned and bold regardless of its name.
    """
    is_quantity = column >= len(IDENTITY_HEADERS)
    if totals:
        return "totals_qty" if is_quantity else "totals"
    if is_quantity:
        return "quantity"
    if heading in QUIET_HEADERS:
        return "quiet"
    if heading in LOUD_HEADERS:
        return "loud"
    if heading in ("S", "M", "B"):
        return "flag"
    return "plain"


def _body_cell(value, heading: str, column: int, column_count: int, totals: bool):
    """One table cell as a wrapping, styled Paragraph."""
    from reportlab.platypus import Paragraph

    style = _paragraph_styles()[_body_cell_style_name(heading, column, totals)]
    return Paragraph(_escape(value), style)


# ─── PDF ─────────────────────────────────────────────────────────────────────

def _pdf_table_style(column_count: int, totals_row: int | None = None):
    """The shared table style. Fonts and colours per cell live in the Paragraphs.

    What is set here is only what a TableStyle can express better than a cell can:
    the header band, the zebra striping, the padding, and the rules.

    The grid is deliberately gone. A full box around every cell of a 117-row table
    is a lot of ink competing with the numbers; horizontal rules alone are enough to
    keep your place on a line, which is the actual job — and the striping already
    does most of that work. Vertical separation comes from the padding.
    """
    from reportlab.lib import colors
    from reportlab.platypus import TableStyle

    rule = colors.Color(0.85, 0.87, 0.90)
    commands = [
        ("BACKGROUND", (0, 0), (-1, 0), colors.Color(*HEADER_RGB)),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        # Room to breathe. The old 2pt default with a 9pt font made the rows read as
        # one grey mass at arm's length.
        ("TOPPADDING", (0, 0), (-1, -1), 4.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4.5),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        # Zebra striping: this is read on a clipboard in a warehouse, where losing
        # your place mid-row is the realistic failure.
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.Color(0.97, 0.975, 0.98)]),
        ("LINEBELOW", (0, 1), (-1, -1), 0.3, rule),
        ("LINEBELOW", (0, 0), (-1, 0), 0.8, colors.Color(*HEADER_RGB)),
    ]
    if totals_row is not None:
        # A rule above the totals, so it reads as a summary and not as one more SKU.
        commands += [
            ("LINEABOVE", (0, totals_row), (-1, totals_row), 0.9, colors.Color(0.3, 0.34, 0.4)),
            ("BACKGROUND", (0, totals_row), (-1, totals_row), colors.Color(0.93, 0.94, 0.96)),
        ]
    return TableStyle(commands)


def _pdf_document(buffer, title: str, subtitle: str, landscape_mode: bool = False):
    """A document plus its heading block.

    The heading is left-aligned rather than centred, and the page number is in the
    footer. Both are for the same reason: these get printed, stapled and put on a
    clipboard, and a centred title with no page number on sheet 3 of 4 is a document
    nobody can tell is incomplete.
    """
    from reportlab.lib import colors, pagesizes
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

    page = pagesizes.landscape(pagesizes.A4) if landscape_mode else pagesizes.A4
    doc = SimpleDocTemplate(
        buffer, pagesize=page,
        leftMargin=10 * mm, rightMargin=10 * mm, topMargin=10 * mm, bottomMargin=14 * mm,
        title=title,
    )
    styles = getSampleStyleSheet()
    elements = [
        Paragraph(
            _escape(title),
            ParagraphStyle(
                "DocTitle", parent=styles["Heading1"], fontSize=15, leading=18,
                spaceAfter=1, textColor=colors.Color(*HEADER_RGB),
            ),
        ),
        Paragraph(
            _escape(subtitle),
            ParagraphStyle(
                "DocSub", parent=styles["Normal"], fontSize=8.5,
                textColor=colors.Color(0.42, 0.45, 0.50),
            ),
        ),
        Spacer(1, 4 * mm),
    ]
    return doc, elements


def _page_footer(canvas, doc):
    """"Page 1 of 3" plus when it was printed, bottom-right on every page.

    Registered as onPage rather than appended as flowables, because the total page
    count is only known once the document has been laid out — and a footer that says
    "page 1" with no total is exactly as useless as no footer when four sheets get
    separated on a warehouse bench.
    """
    from reportlab.lib import colors
    from reportlab.lib.units import mm

    canvas.saveState()
    canvas.setFont("Helvetica", 7.5)
    canvas.setFillColor(colors.Color(0.5, 0.53, 0.58))
    canvas.drawRightString(
        doc.pagesize[0] - 10 * mm, 7 * mm,
        f"Page {canvas.getPageNumber()} · printed {date.today().isoformat()}",
    )
    canvas.restoreState()
