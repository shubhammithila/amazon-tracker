"""Projections — forecast next month sales, calculate ideal stock and reorder alerts."""
import io
import json
import re
import logging
from pathlib import Path

import pandas as pd
from fastapi import APIRouter, Request, Depends, UploadFile, File
from fastapi.responses import JSONResponse, StreamingResponse

from app.routers.auth import require_auth

router = APIRouter(prefix="/projections")
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent.parent
DEFAULTS_FILE = BASE_DIR / "invoice" / "projection_defaults.json"
FAMILIES_FILE = BASE_DIR / "invoice" / "product_families.json"
DATA_FILE = BASE_DIR.parent / "projection_data.json"


def load_defaults() -> dict:
    if DEFAULTS_FILE.exists():
        with open(DEFAULTS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def load_saved_data() -> dict | None:
    if DATA_FILE.exists():
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return None


def save_data(data: dict):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


DEFAULTS = load_defaults()


def load_families() -> dict:
    """Load ASIN -> {parent_product, brand, weight} mapping."""
    if FAMILIES_FILE.exists():
        with open(FAMILIES_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


FAMILIES = load_families()


def calculate_projections(products: list[dict]) -> list[dict]:
    """Run projection formulas on each product row."""
    for p in products:
        last_sale = p.get("last_month_sale", 0) or 0
        seasonal = p.get("seasonal_impact", 1.0) or 1.0
        growth = p.get("growth_rate", 0.3) or 0.0

        # Forecast
        monthly_forecast = last_sale * seasonal * (1 + growth)
        daily_rate = monthly_forecast / 30

        # Lead times
        s2w = p.get("supplier_to_wh", 5) or 0
        pack = p.get("packing", 2) or 0
        w2i = p.get("wh_to_ixd", 10) or 0
        i2f = p.get("ixd_to_fba", 5) or 0
        total_lead = s2w + pack + w2i + i2f
        wh_buffer = p.get("wh_buffer_days", 10) or 0

        # Ideal stock
        ideal_fba = round(daily_rate * total_lead, 1)
        ideal_wh = round(daily_rate * wh_buffer, 1)

        # Current stock
        current_fba = p.get("current_fba_stock", 0) or 0
        current_wh = p.get("current_wh_stock", 0) or 0

        # Alerts
        shipment_alert = round(ideal_fba - current_fba, 1)
        reorder_alert = round(ideal_fba + ideal_wh - current_fba - current_wh, 1)

        # Value
        purchase_rate = p.get("purchase_rate", 0) or 0
        ideal_stock_value = round((ideal_fba + ideal_wh) * purchase_rate, 0)
        current_stock_value = round((current_fba + current_wh) * purchase_rate, 0)

        # Inventory days (how many days current FBA stock lasts)
        inventory_days = round(current_fba / daily_rate, 1) if daily_rate > 0 else 0

        # Update product with calculated fields
        p["monthly_forecast"] = round(monthly_forecast, 1)
        p["daily_rate"] = round(daily_rate, 2)
        p["total_lead_time"] = total_lead
        p["ideal_fba_stock"] = ideal_fba
        p["ideal_wh_stock"] = ideal_wh
        p["shipment_alert"] = shipment_alert
        p["reorder_alert"] = reorder_alert
        p["ideal_stock_value"] = ideal_stock_value
        p["current_stock_value"] = current_stock_value
        p["inventory_days"] = inventory_days

    return products


# ─── CSV Parsing ──────────────────────────────────────────────────────────────

def _clean_number(val) -> float:
    if val is None or str(val).strip() in ("", "-", "nan"):
        return 0.0
    cleaned = re.sub(r"[₹,%\s]", "", str(val))
    try:
        return float(cleaned)
    except ValueError:
        return 0.0


def parse_business_report_for_projections(content: bytes) -> dict[str, float]:
    """
    Parse Business Report CSV → aggregate by parent product → return {product_name: total_kg_sold}.
    Maps ASIN → parent product using FAMILIES, multiplies units × weight to get kg.
    """
    try:
        df = pd.read_csv(io.BytesIO(content))
    except Exception:
        df = pd.read_csv(io.BytesIO(content), encoding="utf-8-sig")

    child_col = "(Child) ASIN"
    if child_col not in df.columns:
        raise ValueError("Could not find '(Child) ASIN' column. Upload Amazon Business Report (By ASIN).")

    # Step 1: Aggregate units by child ASIN (dedup FBA + non-FBA rows)
    asin_units: dict[str, float] = {}
    for _, row in df.iterrows():
        asin = str(row.get(child_col, "")).strip()
        if not asin or len(asin) < 10:
            continue
        units = _clean_number(row.get("Units Ordered", 0))
        asin_units[asin] = asin_units.get(asin, 0) + units

    # Step 2: Map ASINs to parent products and convert units to kg
    product_kg: dict[str, float] = {}
    unmapped = []

    for asin, units in asin_units.items():
        family = FAMILIES.get(asin)
        if not family:
            unmapped.append(asin)
            continue

        parent = family["parent_product"]
        weight = family.get("weight") or 0  # kg per unit

        kg_sold = units * weight
        product_kg[parent] = product_kg.get(parent, 0) + kg_sold

    if unmapped:
        logger.info(f"Projections upload: {len(unmapped)} unmapped ASINs")

    return product_kg


# ─── Endpoints ────────────────────────────────────────────────────────────────

@router.post("/upload-csv")
async def upload_csv(
    request: Request,
    file: UploadFile = File(...),
    _=Depends(require_auth),
):
    """
    Upload Business Report CSV → auto-fill last month sales (in kg) per parent product.
    Returns products array ready for the projections table.
    """
    content = await file.read()
    try:
        product_kg = parse_business_report_for_projections(content)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)

    # Build products list from defaults, filling in last_month_sale from CSV
    products = []
    for name, d in DEFAULTS.items():
        kg_sold = round(product_kg.get(name, 0), 2)
        products.append({
            "product": name,
            "brand": d.get("brand", "Mithila Foods"),
            "last_month_sale": kg_sold,
            "seasonal_impact": d["seasonal_impact"],
            "growth_rate": d["growth_rate"],
            "supplier_to_wh": d["supplier_to_wh"],
            "packing": d["packing"],
            "wh_to_ixd": d["wh_to_ixd"],
            "ixd_to_fba": d["ixd_to_fba"],
            "wh_buffer_days": d["wh_buffer_days"],
            "purchase_rate": d["purchase_rate"],
            "current_fba_stock": 0,
            "current_wh_stock": 0,
        })

    # Run calculations
    products = calculate_projections(products)
    products.sort(key=lambda x: x.get("monthly_forecast", 0), reverse=True)

    # Summary
    total_forecast = sum(p["monthly_forecast"] for p in products)
    filled = sum(1 for p in products if p["last_month_sale"] > 0)

    return JSONResponse({
        "products": products,
        "total_products": len(products),
        "filled_from_csv": filled,
        "total_kg_from_csv": round(sum(product_kg.values()), 1),
        "total_forecast_kg": round(total_forecast, 0),
    })


@router.get("/defaults")
async def get_defaults(request: Request, _=Depends(require_auth)):
    """Get all product defaults (lead times, rates, seasonal factors)."""
    return JSONResponse(DEFAULTS)


@router.get("/last")
async def get_last_projection(request: Request, _=Depends(require_auth)):
    """Get last saved projection data."""
    data = load_saved_data()
    if not data:
        return JSONResponse({"products": [], "globals": {}})
    return JSONResponse(data)


@router.post("/calculate")
async def calculate(request: Request, _=Depends(require_auth)):
    """
    Receive products array with user-edited values, calculate projections, save and return.
    Body: { globals: {...}, products: [{product, last_month_sale, seasonal_impact, ...}] }
    """
    body = await request.json()
    products = body.get("products", [])
    globals_data = body.get("globals", {})

    # Apply global defaults to products that don't have overrides
    global_growth = globals_data.get("growth_rate", 0.3)
    global_seasonal = globals_data.get("seasonal_impact", 1.0)

    for p in products:
        if p.get("growth_rate") is None:
            p["growth_rate"] = global_growth
        if p.get("seasonal_impact") is None:
            p["seasonal_impact"] = global_seasonal

    # Run calculations
    products = calculate_projections(products)

    # Sort by monthly forecast desc
    products.sort(key=lambda x: x.get("monthly_forecast", 0), reverse=True)

    # Summary
    total_forecast = sum(p["monthly_forecast"] for p in products)
    total_ideal_value = sum(p["ideal_stock_value"] for p in products)
    total_current_value = sum(p["current_stock_value"] for p in products)
    ship_alerts = sum(1 for p in products if p["shipment_alert"] > 0)
    reorder_alerts = sum(1 for p in products if p["reorder_alert"] > 0)
    critical_alerts = sum(1 for p in products if p.get("inventory_days", 999) < 7)

    result = {
        "globals": globals_data,
        "products": products,
        "summary": {
            "total_products": len(products),
            "total_forecast_kg": round(total_forecast, 0),
            "total_ideal_value": round(total_ideal_value, 0),
            "total_current_value": round(total_current_value, 0),
            "shipment_alerts": ship_alerts,
            "reorder_alerts": reorder_alerts,
            "critical_alerts": critical_alerts,
        },
    }

    # Save
    save_data(result)

    return JSONResponse(result)


@router.post("/init")
async def init_from_defaults(request: Request, _=Depends(require_auth)):
    """Initialize projection table with all products from defaults (empty sales data)."""
    products = []
    for name, d in DEFAULTS.items():
        products.append({
            "product": name,
            "brand": d.get("brand", "Mithila Foods"),
            "last_month_sale": 0,
            "seasonal_impact": d["seasonal_impact"],
            "growth_rate": d["growth_rate"],
            "supplier_to_wh": d["supplier_to_wh"],
            "packing": d["packing"],
            "wh_to_ixd": d["wh_to_ixd"],
            "ixd_to_fba": d["ixd_to_fba"],
            "wh_buffer_days": d["wh_buffer_days"],
            "purchase_rate": d["purchase_rate"],
            "current_fba_stock": 0,
            "current_wh_stock": 0,
        })

    products.sort(key=lambda x: x["product"].lower())
    return JSONResponse({"products": products})


@router.delete("/clear")
async def clear_projection(request: Request, _=Depends(require_auth)):
    """Clear saved projection data."""
    if DATA_FILE.exists():
        DATA_FILE.unlink()
    return JSONResponse({"status": "cleared"})


@router.get("/download")
async def download_projection(request: Request, _=Depends(require_auth)):
    """Download last projection as Excel."""
    data = load_saved_data()
    if not data or not data.get("products"):
        return JSONResponse({"error": "No projection data"}, status_code=404)

    products = data["products"]
    rows = [{
        "Product": p["product"],
        "Brand": p.get("brand", ""),
        "Last Month Sale (kg)": p.get("last_month_sale", 0),
        "Seasonal Impact": p.get("seasonal_impact", 1),
        "Growth Rate": p.get("growth_rate", 0.3),
        "Monthly Forecast (kg)": p.get("monthly_forecast", 0),
        "Daily Rate (kg)": p.get("daily_rate", 0),
        "Lead Time (days)": p.get("total_lead_time", 0),
        "Ideal FBA Stock (kg)": p.get("ideal_fba_stock", 0),
        "Current FBA Stock (kg)": p.get("current_fba_stock", 0),
        "Shipment Alert (kg)": p.get("shipment_alert", 0),
        "WH Buffer Days": p.get("wh_buffer_days", 0),
        "Ideal WH Stock (kg)": p.get("ideal_wh_stock", 0),
        "Current WH Stock (kg)": p.get("current_wh_stock", 0),
        "Reorder Alert (kg)": p.get("reorder_alert", 0),
        "Purchase Rate (Rs/kg)": p.get("purchase_rate", 0),
        "Ideal Stock Value (Rs)": p.get("ideal_stock_value", 0),
        "Inventory Days": p.get("inventory_days", 0),
    } for p in products]

    df = pd.DataFrame(rows)
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Projections")

        # Color alerts
        ws = writer.sheets["Projections"]
        from openpyxl.styles import PatternFill, Font
        red_fill = PatternFill(start_color="FFC7CE", end_color="FFC7CE", fill_type="solid")
        yellow_fill = PatternFill(start_color="FFEB9C", end_color="FFEB9C", fill_type="solid")
        # Shipment Alert column (K = 11)
        for row_idx, p in enumerate(products, start=2):
            if p.get("shipment_alert", 0) > 0:
                ws.cell(row=row_idx, column=11).fill = red_fill
            if p.get("reorder_alert", 0) > 0:
                ws.cell(row=row_idx, column=15).fill = red_fill
            if p.get("inventory_days", 999) < 7:
                ws.cell(row=row_idx, column=18).fill = red_fill
            elif p.get("inventory_days", 999) < 14:
                ws.cell(row=row_idx, column=18).fill = yellow_fill

    buffer.seek(0)
    return StreamingResponse(
        buffer,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=Projections.xlsx"},
    )
