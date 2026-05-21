"""FBA Invoice generation endpoints."""
import json
from datetime import datetime

from fastapi import APIRouter, Request, Depends, UploadFile, File
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc

from app.database import get_db
from app.models import Invoice
from app.routers.auth import require_auth
from app.invoice.parser import parse_shipment_tsv
from app.invoice.generator import generate_excel_invoice, generate_pdf_invoice
from app.invoice.hsn_codes import save_invoice_hsn_codes
from app.invoice.company_data import (
    SUPPLIER_NAME, SUPPLIER_ADDRESS, SUPPLIER_GSTIN, TRANSPORTERS,
    get_next_invoice_number,
)

router = APIRouter(prefix="/invoice")

LAST_KNOWN_INVOICE = 27  # Last invoice was ST/26-27/027


@router.post("/parse-shipment")
async def parse_shipment(request: Request, file: UploadFile = File(...), _=Depends(require_auth)):
    """Parse an uploaded Amazon FBA shipment TSV file."""
    content = await file.read()
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        text = content.decode("utf-8-sig", errors="replace")

    result = parse_shipment_tsv(text)
    return JSONResponse(result)


@router.get("/next-number")
async def get_next_number(request: Request, _=Depends(require_auth), db: AsyncSession = Depends(get_db)):
    """Get the next invoice number."""
    last_invoice = (await db.execute(
        select(Invoice).order_by(desc(Invoice.invoice_number)).limit(1)
    )).scalar_one_or_none()

    last_num = last_invoice.invoice_number if last_invoice else LAST_KNOWN_INVOICE
    next_no = get_next_invoice_number(last_num)

    return JSONResponse({"next_number": next_no, "last_number": last_num})


@router.get("/transporters")
async def get_transporters(request: Request, _=Depends(require_auth)):
    """Get list of available transporters."""
    return JSONResponse({"transporters": TRANSPORTERS})


@router.get("/history")
async def invoice_history(request: Request, _=Depends(require_auth), db: AsyncSession = Depends(get_db)):
    """Get all saved invoices."""
    invoices = (await db.execute(
        select(Invoice).order_by(desc(Invoice.created_at))
    )).scalars().all()

    return JSONResponse([
        {
            "id": inv.id,
            "invoice_no": inv.invoice_no,
            "shipment_id": inv.shipment_id,
            "date": inv.date,
            "fc_code": inv.fc_code,
            "recipient_state": inv.recipient_state,
            "transporter": inv.transporter,
            "total_qty": inv.total_qty,
            "total_amount": float(inv.total_amount) if inv.total_amount else 0,
            "created_at": inv.created_at.isoformat() if inv.created_at else "",
        }
        for inv in invoices
    ])


@router.post("/save")
async def save_invoice(request: Request, _=Depends(require_auth), db: AsyncSession = Depends(get_db)):
    """Save a finalized invoice to database."""
    data = await request.json()

    # Determine invoice number — use user-provided if edited, else auto-generate
    user_invoice_no = data.get("details", {}).get("invoice_no", "").strip()

    last_invoice = (await db.execute(
        select(Invoice).order_by(desc(Invoice.invoice_number)).limit(1)
    )).scalar_one_or_none()

    last_num = last_invoice.invoice_number if last_invoice else LAST_KNOWN_INVOICE
    next_num = last_num + 1

    if user_invoice_no:
        # User edited the invoice number — use it, try to extract the seq number
        invoice_no = user_invoice_no
        import re
        seq_match = re.search(r"/(\d+)$", user_invoice_no)
        if seq_match:
            next_num = int(seq_match.group(1))
    else:
        invoice_no = get_next_invoice_number(last_num)

    # Calculate totals
    total_qty = 0
    total_taxable = 0
    total_igst = 0
    total_amount = 0
    for item in data.get("items", []):
        qty = item.get("quantity", 0)
        rate = item.get("rate", 0)
        gst_rate = item.get("gst_rate", 5)
        taxable = qty * rate
        igst = taxable * gst_rate / 100
        total_qty += qty
        total_taxable += taxable
        total_igst += igst
        total_amount += taxable + igst

    details = data.get("details", {})

    inv = Invoice(
        invoice_no=invoice_no,
        invoice_number=next_num,
        shipment_id=details.get("shipment_id", ""),
        date=details.get("date", ""),
        supplier_gstin=data.get("supplier", {}).get("gstin", SUPPLIER_GSTIN),
        recipient_gstin=data.get("recipient", {}).get("gstin", ""),
        recipient_state=details.get("place_of_supply", ""),
        fc_code=details.get("fc_code", ""),
        transporter=details.get("transporter", ""),
        total_qty=total_qty,
        total_taxable=round(total_taxable, 2),
        total_igst=round(total_igst, 2),
        total_amount=round(total_amount, 2),
        invoice_data=json.dumps(data),
    )
    db.add(inv)
    await db.commit()

    # Save HSN codes to master database so they're remembered for next time
    save_invoice_hsn_codes(data.get("items", []))

    return JSONResponse({"invoice_no": invoice_no, "id": inv.id})


@router.post("/generate-excel")
async def generate_excel(request: Request, _=Depends(require_auth)):
    """Generate Excel invoice from submitted invoice data."""
    data = await request.json()
    buffer = generate_excel_invoice(data)
    shipment_id = data.get("details", {}).get("shipment_id", "invoice")
    filename = f"FBA_Invoice_{shipment_id}.xlsx"

    return StreamingResponse(
        buffer,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@router.post("/generate-pdf")
async def generate_pdf(request: Request, _=Depends(require_auth)):
    """Generate PDF invoice from submitted invoice data."""
    data = await request.json()
    buffer = generate_pdf_invoice(data)
    shipment_id = data.get("details", {}).get("shipment_id", "invoice")
    filename = f"FBA_Invoice_{shipment_id}.pdf"

    return StreamingResponse(
        buffer,
        media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )
