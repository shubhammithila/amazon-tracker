"""Shipment Maker — upload sales + stock CSVs, generate packing plan, track daily packing."""
import io
import json
import re
import logging
from pathlib import Path

import pandas as pd
from fastapi import APIRouter, Request, Depends, UploadFile, File, Form
from fastapi.responses import JSONResponse, StreamingResponse

from app.routers.auth import require_auth

router = APIRouter(prefix="/shipment")
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent.parent
FAMILIES_FILE = BASE_DIR / "invoice" / "product_families.json"
DATA_FILE = BASE_DIR.parent / "shipment_plan.json"


def load_families() -> dict:
    if FAMILIES_FILE.exists():
        with open(FAMILIES_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


FAMILIES = load_families()


def _clean_number(val) -> float:
    if val is None or str(val).strip() in ("", "-", "nan"):
        return 0.0
    cleaned = re.sub(r"[₹,%\s]", "", str(val))
    try:
        return float(cleaned)
    except ValueError:
        return 0.0


def parse_sales_csv(content: bytes) -> dict[str, int]:
    """Parse Business Report CSV → {ASIN: total_units_ordered}."""
    try:
        df = pd.read_csv(io.BytesIO(content))
    except Exception:
        df = pd.read_csv(io.BytesIO(content), encoding="utf-8-sig")

    child_col = "(Child) ASIN"
    if child_col not in df.columns:
        raise ValueError("Could not find '(Child) ASIN' column.")

    asin_units: dict[str, int] = {}
    for _, row in df.iterrows():
        asin = str(row.get(child_col, "")).strip()
        if not asin or len(asin) < 10:
            continue
        units = int(_clean_number(row.get("Units Ordered", 0)))
        asin_units[asin] = asin_units.get(asin, 0) + units

    return asin_units


def parse_stock_csv(content: bytes) -> dict[str, int]:
    """Parse FBA stock report CSV → {ASIN: total_stock}. Sums: fulfillable + reserved + inbound columns."""
    try:
        df = pd.read_csv(io.BytesIO(content))
    except Exception:
        df = pd.read_csv(io.BytesIO(content), encoding="utf-8-sig")

    if "asin" not in df.columns:
        raise ValueError("Could not find 'asin' column in stock report.")

    stock_cols = [
        "afn-fulfillable-quantity", "afn-reserved-quantity",
        "afn-inbound-working-quantity", "afn-inbound-shipped-quantity",
        "afn-inbound-receiving-quantity", "afn-researching-quantity",
        "afn-reserved-future-supply", "afn-future-supply-buyable",
    ]
    existing_cols = [c for c in stock_cols if c in df.columns]

    asin_stock: dict[str, int] = {}
    for _, row in df.iterrows():
        asin = str(row.get("asin", "")).strip()
        if not asin or len(asin) < 10:
            continue
        total = sum(int(_clean_number(row.get(c, 0))) for c in existing_cols)
        asin_stock[asin] = total

    return asin_stock


# ─── Endpoints ────────────────────────────────────────────────────────────────

@router.post("/generate")
async def generate_plan(
    request: Request,
    sales_csv: UploadFile = File(...),
    stock_csv: UploadFile = File(...),
    multiplier: float = Form(5.0),
    _=Depends(require_auth),
):
    """Upload sales + stock CSVs → generate shipment plan."""
    sales_content = await sales_csv.read()
    stock_content = await stock_csv.read()

    try:
        sales = parse_sales_csv(sales_content)
    except Exception as e:
        return JSONResponse({"error": f"Sales CSV error: {e}"}, status_code=400)

    try:
        stock = parse_stock_csv(stock_content)
    except Exception as e:
        return JSONResponse({"error": f"Stock CSV error: {e}"}, status_code=400)

    # Build plan from FAMILIES (master product list)
    plan = []
    for asin, info in FAMILIES.items():
        units_sold = sales.get(asin, 0)
        fba_stock = stock.get(asin, 0)
        projection = int(units_sold * multiplier)
        deficit = projection - fba_stock

        plan.append({
            "brand": "MF" if info["brand"] == "Mithila Foods" else "HF",
            "fba_sku": "",  # Will be filled from SKU data
            "asin": asin,
            "item": info["parent_product"],
            "weight": info.get("weight") or 0,
            "sales_7d": units_sold,
            "projection": projection,
            "fba_stock": fba_stock,
            "deficit": deficit,
            "shipment_plan": max(0, deficit),  # Default: ship the deficit (0 if negative)
            "available": 0,
            "day1": 0, "day2": 0, "day3": 0, "day4": 0, "day5": 0, "day6": 0,
            "s": False, "m": False, "b": False,
        })

    # Try to get FBA SKU from stock CSV (sku column)
    try:
        stock_df = pd.read_csv(io.BytesIO(stock_content))
        if "sku" in stock_df.columns and "asin" in stock_df.columns:
            sku_map = dict(zip(stock_df["asin"].astype(str), stock_df["sku"].astype(str)))
            for p in plan:
                if not p["fba_sku"]:
                    p["fba_sku"] = sku_map.get(p["asin"], "")
    except Exception:
        pass

    # Sort: positive deficit first (descending), then by item name
    plan.sort(key=lambda x: (-x["deficit"], x["item"], x["weight"]))

    # Save
    data = {
        "multiplier": multiplier,
        "plan": plan,
        "created_at": pd.Timestamp.now().isoformat(),
    }
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)

    return JSONResponse(data)


@router.get("/last")
async def get_last_plan(request: Request, _=Depends(require_auth)):
    """Get last saved shipment plan."""
    if not DATA_FILE.exists():
        return JSONResponse({"plan": [], "multiplier": 5.0})
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        return JSONResponse(json.load(f))


@router.post("/save")
async def save_plan(request: Request, _=Depends(require_auth)):
    """Save updated plan (after editing in UI)."""
    body = await request.json()
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(body, f, ensure_ascii=False)
    return JSONResponse({"status": "saved"})


@router.delete("/clear")
async def clear_plan(request: Request, _=Depends(require_auth)):
    if DATA_FILE.exists():
        DATA_FILE.unlink()
    return JSONResponse({"status": "cleared"})


@router.get("/download-packing-plan")
async def download_packing_plan(request: Request, _=Depends(require_auth)):
    """Download packing plan as Excel (for warehouse team)."""
    if not DATA_FILE.exists():
        return JSONResponse({"error": "No plan"}, status_code=404)

    with open(DATA_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)

    items = [p for p in data["plan"] if p.get("shipment_plan", 0) > 0]

    rows = [{
        "S": "",
        "M": "",
        "B": "",
        "Brand": p["brand"],
        "FBA SKU": p["fba_sku"],
        "ASIN": p["asin"],
        "Item": p["item"],
        "Weight": f"{p['weight']}kg" if p["weight"] else "",
        "Shipment": p["shipment_plan"],
        "Avl": p.get("available", ""),
        "Day 1": p.get("day1", ""),
        "Day 2": p.get("day2", ""),
        "Day 3": p.get("day3", ""),
        "Day 4": p.get("day4", ""),
        "Day 5": p.get("day5", ""),
        "Day 6": p.get("day6", ""),
    } for p in items]

    df = pd.DataFrame(rows)
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Packing Plan")
        ws = writer.sheets["Packing Plan"]
        # Set column widths
        widths = [4, 4, 4, 6, 18, 12, 20, 8, 10, 6, 7, 7, 7, 7, 7, 7]
        from openpyxl.utils import get_column_letter
        for i, w in enumerate(widths, 1):
            ws.column_dimensions[get_column_letter(i)].width = w

    buffer.seek(0)
    return StreamingResponse(
        buffer,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=FBA_Packing_Plan.xlsx"},
    )


@router.get("/download-shipment-file")
async def download_shipment_file(
    request: Request,
    mode: str = "remaining",  # "remaining" or "all"
    _=Depends(require_auth),
):
    """Download FBA SKU + Units for Amazon shipment creation."""
    if not DATA_FILE.exists():
        return JSONResponse({"error": "No plan"}, status_code=404)

    with open(DATA_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)

    rows = []
    for p in data["plan"]:
        ship = p.get("shipment_plan", 0)
        if ship <= 0:
            continue

        packed = sum(p.get(f"day{d}", 0) or 0 for d in range(1, 7))
        remaining = ship - packed

        if mode == "remaining" and remaining <= 0:
            continue

        qty = remaining if mode == "remaining" else ship
        if qty <= 0:
            continue

        rows.append({
            "sku": p["fba_sku"] or p["asin"],
            "quantity": qty,
        })

    df = pd.DataFrame(rows)
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Shipment")
    buffer.seek(0)

    fname = f"Shipment_{'Remaining' if mode == 'remaining' else 'Full'}.xlsx"
    return StreamingResponse(
        buffer,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename={fname}"},
    )
