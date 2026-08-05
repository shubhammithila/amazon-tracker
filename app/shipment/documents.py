"""The four shipment downloads. Each returns an in-memory io.BytesIO.

The documents:

  packing plan   xlsx + pdf   the full plan, for the owner
  remaining      pdf          the morning clipboard sheet, for the packer
  packed         xlsx         daily units AND cartons, for building the shipment
  shipment file  xlsx         the merchant-SKU + quantity upload for Amazon

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
from datetime import date, datetime

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

    Kept as a thin alias because this module's builders call it in six places and
    the name reads better in a row-building expression. The RULE lives in logic.py
    with the other shared rules: grams under 1 kg, kilos above, and one
    implementation so the printed sheet cannot disagree with the screen.
    """
    return logic.weight_label(weight)


def _flags(item) -> str:
    """The S/M/B carton-size flags as a compact string."""
    return "".join(
        letter
        for letter, present in (("S", item.get("s")), ("M", item.get("m")), ("B", item.get("b")))
        if present
    )


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


def build_packing_plan_xlsx(plan: dict, items: list[dict]) -> io.BytesIO:
    """The plan as Excel. `items` must already be in canonical order."""
    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Packing Plan"

    # "In Stock" and "To Make" are here because the owner plans production from
    # this sheet. Without them the In-stock figure he typed lives only on screen,
    # which is how it came to be a column that fed nothing at all.
    headers = [
        "Brand", "Product", "Weight", "ASIN", "Merchant SKU",
        "7d Sales", "Projection", "FBA Stock", "Deficit",
        "To Ship", "In Stock", "Packed", "To Pack", "To Make", "Sizes",
    ]
    rows = [
        [
            item.get("brand", ""),
            item.get("item", ""),
            _weight_label(item.get("weight")),
            item.get("asin", ""),
            item.get("fba_sku", ""),
            int(item.get("sales_7d") or 0),
            int(item.get("projection") or 0),
            int(item.get("fba_stock") or 0),
            int(item.get("deficit") or 0),
            int(item.get("shipment_plan") or 0),
            int(item.get("available") or 0),
            int(item.get("packed") or 0),
            int(item.get("remaining") or 0),
            int(item.get("to_source") or 0),
            _flags(item),
        ]
        for item in items
    ]
    _write_sheet(
        sheet, headers, rows,
        [8, 30, 9, 14, 20, 10, 11, 11, 10, 10, 10, 10, 10, 10, 8],
    )

    buffer = io.BytesIO()
    workbook.save(buffer)
    buffer.seek(0)
    return buffer


def build_packed_xlsx(plan: dict, items: list[dict], days: list[dict]) -> io.BytesIO:
    """Daily packed units and cartons — requirements 6 and 7.

    One column pair per packing date, in chronological order, so the owner can
    see what was boxed on each day rather than only a total. Cartons are here
    because they prefill the invoice's Boxes field, which is the concrete payoff
    of requirement 7.

    Held days are included and labelled. Their boxes exist, so leaving them out
    would make the sheet disagree with the warehouse floor; the label is what
    stops them being read as ready to ship.
    """
    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Packed Daily"

    headers = ["Brand", "Product", "Weight", "ASIN", "Merchant SKU", "To Ship"]
    for day in days:
        label = day.get("pack_date", "")
        status = day.get("status", "")
        suffix = f" ({status})" if status in (logic.STATUS_HELD, logic.STATUS_OPEN) else ""
        headers.append(f"{label} Units{suffix}")
        headers.append(f"{label} Cartons{suffix}")
    # "To Pack", matching the plan sheet and the screen. Both sheets used to say
    # "Remaining", which invited reading one as the other — and on this sheet it
    # means units still to BOX, which is not the same question as the plan
    # sheet's "To Make".
    headers += ["Total Units", "Total Cartons", "Shippable Units", "To Pack"]

    # ASIN -> {units, cartons} per day, so a SKU untouched on a day reads 0
    # rather than shifting the later columns left.
    per_day: list[dict[str, dict]] = []
    for day in days:
        per_day.append({e.get("asin"): e for e in day.get("entries") or []})

    shippable = logic.shippable_units_by_asin(days)

    rows = []
    for item in items:
        asin = item.get("asin", "")
        row = [
            item.get("brand", ""),
            item.get("item", ""),
            _weight_label(item.get("weight")),
            asin,
            item.get("fba_sku", ""),
            int(item.get("shipment_plan") or 0),
        ]
        total_units = total_cartons = 0
        for entries in per_day:
            entry = entries.get(asin) or {}
            units = int(entry.get("units") or 0)
            cartons = int(entry.get("cartons") or 0)
            total_units += units
            total_cartons += cartons
            row.append(units)
            row.append(cartons)
        row += [
            total_units,
            total_cartons,
            int(shippable.get(asin, 0)),
            logic.remaining_for(item.get("shipment_plan"), total_units),
        ]
        rows.append(row)

    widths = [8, 30, 9, 14, 20, 10] + [12, 12] * len(days) + [12, 13, 15, 11]
    _write_sheet(sheet, headers, rows, widths)

    buffer = io.BytesIO()
    workbook.save(buffer)
    buffer.seek(0)
    return buffer


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


# ─── PDF ─────────────────────────────────────────────────────────────────────

def _pdf_table_style(column_count: int):
    from reportlab.lib import colors
    from reportlab.platypus import TableStyle

    return TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.Color(*HEADER_RGB)),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTNAME", (0, 1), (-1, -1), "Helvetica"),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.grey),
        # Zebra striping: this is read on a clipboard in a warehouse, where
        # losing your place mid-row is the realistic failure.
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.Color(0.96, 0.96, 0.96)]),
        ("ALIGN", (column_count - 1, 1), (-1, -1), "RIGHT"),
    ])


def _pdf_document(buffer, title: str, subtitle: str, landscape_mode: bool = False):
    from reportlab.lib import pagesizes
    from reportlab.lib.enums import TA_CENTER
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

    page = pagesizes.landscape(pagesizes.A4) if landscape_mode else pagesizes.A4
    doc = SimpleDocTemplate(
        buffer, pagesize=page,
        leftMargin=10 * mm, rightMargin=10 * mm, topMargin=10 * mm, bottomMargin=10 * mm,
        title=title,
    )
    styles = getSampleStyleSheet()
    elements = [
        Paragraph(
            title,
            ParagraphStyle("DocTitle", parent=styles["Heading1"], fontSize=14, alignment=TA_CENTER),
        ),
        Paragraph(
            subtitle,
            ParagraphStyle("DocSub", parent=styles["Normal"], fontSize=8, alignment=TA_CENTER),
        ),
        Spacer(1, 4 * mm),
    ]
    return doc, elements


def build_packing_plan_pdf(plan: dict, items: list[dict]) -> io.BytesIO:
    """The plan as PDF. Landscape, because it carries the full CSV-derived detail."""
    from reportlab.lib.units import mm
    from reportlab.platypus import Table

    buffer = io.BytesIO()
    doc, elements = _pdf_document(
        buffer,
        "Weekly Shipment Plan",
        f"{plan.get('label') or 'Plan'} — generated {datetime.utcnow():%Y-%m-%d %H:%M} UTC",
        landscape_mode=True,
    )

    data = [["Brand", "Product", "Wt", "ASIN", "Merchant SKU",
             "7d", "Proj", "Stock", "To Ship", "Packed", "Left"]]
    for item in items:
        data.append([
            item.get("brand", ""),
            item.get("item", ""),
            _weight_label(item.get("weight")),
            item.get("asin", ""),
            item.get("fba_sku", ""),
            int(item.get("sales_7d") or 0),
            int(item.get("projection") or 0),
            int(item.get("fba_stock") or 0),
            int(item.get("shipment_plan") or 0),
            int(item.get("packed") or 0),
            int(item.get("remaining") or 0),
        ])

    table = Table(
        data, repeatRows=1,
        colWidths=[14 * mm, 62 * mm, 12 * mm, 24 * mm, 38 * mm,
                   13 * mm, 15 * mm, 16 * mm, 18 * mm, 17 * mm, 14 * mm],
    )
    table.setStyle(_pdf_table_style(6))
    elements.append(table)
    doc.build(elements)
    buffer.seek(0)
    return buffer


def build_remaining_pdf(
    plan: dict, items: list[dict], pack_date: str | None = None
) -> io.BytesIO:
    """The morning sheet: everything still outstanding against the plan.

    Portrait A4 on purpose — this is printed and carried around the warehouse, not
    studied at a desk, so it needs to read as a clipboard page. The columns are
    only what the packer needs: what it is, how many are left, and blanks to write
    in what was actually boxed.

    Rows with nothing remaining are omitted. That is the point of the document:
    the packer sees a list that shrinks each morning as work gets done, and a
    fully-packed SKU dropping off is the feedback that it was recorded.
    """
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, Spacer, Table

    outstanding = [item for item in items if int(item.get("remaining") or 0) > 0]

    buffer = io.BytesIO()
    doc, elements = _pdf_document(
        buffer,
        "Still To Pack",
        f"{plan.get('label') or 'Plan'} — as of {pack_date or date.today().isoformat()}",
    )

    if not outstanding:
        from reportlab.lib.styles import getSampleStyleSheet

        elements.append(Spacer(1, 10 * mm))
        elements.append(
            Paragraph(
                "Nothing outstanding — the whole plan is packed.",
                getSampleStyleSheet()["Normal"],
            )
        )
        doc.build(elements)
        buffer.seek(0)
        return buffer

    data = [["Product", "Weight", "Merchant SKU", "To Ship", "Packed", "Left", "Units", "Cartons"]]
    for item in outstanding:
        data.append([
            item.get("item", ""),
            _weight_label(item.get("weight")),
            item.get("fba_sku", ""),
            int(item.get("shipment_plan") or 0),
            int(item.get("packed") or 0),
            int(item.get("remaining") or 0),
            "",  # written in by hand on the floor
            "",
        ])

    table = Table(
        data, repeatRows=1,
        colWidths=[52 * mm, 15 * mm, 32 * mm, 16 * mm, 16 * mm, 13 * mm, 18 * mm, 18 * mm],
    )
    table.setStyle(_pdf_table_style(4))
    elements.append(table)

    elements.append(Spacer(1, 4 * mm))
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet

    note = ParagraphStyle(
        "Note", parent=getSampleStyleSheet()["Normal"], fontSize=7.5, textColor="#555555"
    )
    total_left = sum(int(i.get("remaining") or 0) for i in outstanding)
    elements.append(
        Paragraph(
            f"{len(outstanding)} SKUs, {total_left} units still to pack. "
            "Write the units and cartons packed in the last two columns, then enter "
            "them on the Packing screen.",
            note,
        )
    )

    doc.build(elements)
    buffer.seek(0)
    return buffer
