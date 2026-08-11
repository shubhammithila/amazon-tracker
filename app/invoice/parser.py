"""Parse Amazon FBA shipment TSV files with auto-pricing and FC lookup."""
import csv
import json
import os
import re
from pathlib import Path
from typing import Optional

from app.invoice.hsn_codes import lookup_hsn
from app.invoice.company_data import get_gstin_for_state, SUPPLIER_GSTIN, PRIORITY_FC_ADDRESSES

BASE_DIR = Path(__file__).parent


def load_fc_addresses() -> dict:
    """Load FC address data from JSON."""
    path = BASE_DIR / "fc_addresses.json"
    if path.exists():
        with open(path, "r") as f:
            return json.load(f)
    return {}


def load_pricing() -> dict:
    """Load pricing data (SKU/ASIN → purchase rate)."""
    path = BASE_DIR / "pricing_data.json"
    if path.exists():
        with open(path, "r") as f:
            return json.load(f)
    return {}


def load_pack_weights() -> dict:
    """{ASIN: pack size in kg}, for totalling a shipment's net weight.

    Read from ``product_families.json`` rather than the live MRP sheet, deliberately:
    this parser is synchronous and runs while handling an upload, so a network fetch
    here would make the invoice screen wait on Google and fail when Google is
    unreachable. The Shipment tab already reads the sheet live, so a newly added product
    reaches its plan; this path is the manual fallback, where a months-old pack size is
    still far better than no weight at all.
    """
    path = BASE_DIR / "product_families.json"
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            families = json.load(f)
    except Exception:
        return {}

    weights: dict = {}
    for asin, info in families.items():
        try:
            weight = float((info or {}).get("weight") or 0)
        except (TypeError, ValueError):
            continue
        if weight > 0:
            weights[str(asin).strip().upper()] = weight
    return weights


FC_ADDRESSES = load_fc_addresses()
PRICING = load_pricing()
PACK_WEIGHTS = load_pack_weights()


def get_pack_weight(sku: str, asin: str) -> float:
    """Pack size in kg for one line, or 0.0 when the catalogue does not know it.

    ASIN is tried first because that is what the catalogue is keyed by; the SKU is a
    fallback for exports where the ASIN column is blank.

    Returns 0.0 rather than guessing. ``logic.shipment_weight`` counts those lines
    separately and the screen says how many are missing, so a short total is VISIBLE
    instead of looking like a complete answer.
    """
    for key in (asin, sku):
        if not key:
            continue
        found = PACK_WEIGHTS.get(str(key).strip().upper())
        if found:
            return found
    return 0.0


def get_fc_info(code: str) -> dict:
    """Get full FC address info by FC code. Priority addresses checked first."""
    code = code.upper().strip()

    # Check priority addresses first (exact addresses you use most)
    if code in PRIORITY_FC_ADDRESSES:
        pfc = PRIORITY_FC_ADDRESSES[code]
        state = pfc["state"]
        name = pfc.get("name", "")
        address = pfc["address"]
        # Full address includes company name + address
        full_address = f"{name}\n{address}" if name else address
        return {
            "fc_code": code,
            "state": state,
            "city": "",
            "pincode": pfc.get("pincode", ""),
            "full_address": full_address,
            "recipient_gstin": get_gstin_for_state(state),
        }

    if code in FC_ADDRESSES:
        fc = FC_ADDRESSES[code]
        state = fc.get("state", "")
        building = fc.get("building", "")
        road = fc.get("road", "")
        city = fc.get("city", "")
        district = fc.get("district", "")
        pincode = fc.get("pincode", "").rstrip(",").strip()

        # Build full address
        parts = [p for p in [building, road, city, district, state, pincode] if p]
        full_address = ", ".join(parts)

        return {
            "fc_code": code,
            "state": state,
            "city": city or district,
            "pincode": pincode,
            "full_address": full_address,
            "recipient_gstin": get_gstin_for_state(state),
        }
    return {
        "fc_code": code,
        "state": "",
        "city": code,
        "pincode": "",
        "full_address": f"Amazon FC {code}",
        "recipient_gstin": "",
    }


def get_purchase_rate(sku: str, asin: str) -> float:
    """Look up purchase rate by SKU or ASIN."""
    # Try exact SKU match first
    if sku in PRICING:
        return PRICING[sku]
    # Try ASIN
    if asin in PRICING:
        return PRICING[asin]
    # Try SKU without "FBA" suffix (some have trailing " FBA")
    sku_clean = sku.replace(" FBA", "").strip()
    if sku_clean in PRICING:
        return PRICING[sku_clean]
    return 0


def parse_shipment_tsv(content: str) -> dict:
    """
    Parse Amazon FBA shipment TSV file.
    Returns shipment metadata and line items with auto-filled pricing.
    """
    lines = content.strip().split("\n")

    # Parse header metadata (first 7 lines)
    metadata = {
        "shipment_id": "",
        "name": "",
        "ship_to": "",
        "total_skus": 0,
        "total_units": 0,
    }

    for line in lines[:7]:
        parts = line.split("\t")
        if len(parts) >= 2:
            key = parts[0].strip().lower()
            val = parts[1].strip()
            if key == "shipment id":
                metadata["shipment_id"] = val
            elif key == "name":
                metadata["name"] = val
            elif key == "ship to":
                metadata["ship_to"] = val
            elif key == "total skus":
                metadata["total_skus"] = int(val) if val.isdigit() else 0
            elif key == "total units":
                metadata["total_units"] = int(val) if val.isdigit() else 0

    # Extract warehouse code from ship_to or name
    warehouse_code = metadata["ship_to"]
    if not warehouse_code:
        match = re.search(r"-([A-Z]{2,4}\d[A-Z0-9]*)\s*$", metadata["name"])
        if match:
            warehouse_code = match.group(1)

    # Get full FC info
    fc_info = get_fc_info(warehouse_code)
    metadata["warehouse"] = fc_info
    metadata["recipient_gstin"] = fc_info["recipient_gstin"]
    metadata["supplier_gstin"] = SUPPLIER_GSTIN

    # Parse item lines (after the header row)
    items = []
    header_idx = None
    for i, line in enumerate(lines):
        if "Merchant SKU" in line and "Title" in line:
            header_idx = i
            break

    if header_idx is None:
        return {"metadata": metadata, "items": [], "error": "Could not find header row"}

    reader = csv.DictReader(
        lines[header_idx:],
        delimiter="\t",
        quoting=csv.QUOTE_ALL,
    )

    for row in reader:
        sku = row.get("Merchant SKU", "").strip()
        title = row.get("Title", "").strip()
        asin = row.get("ASIN", "").strip()
        fnsku = row.get("FNSKU", "").strip()
        shipped = row.get("Shipped", "0").strip()

        if not sku or not title:
            continue

        try:
            quantity = int(shipped)
        except ValueError:
            quantity = 0

        if quantity <= 0:
            continue

        # Lookup HSN code based on title
        hsn_info = lookup_hsn(title, sku=sku)

        # Auto-fill purchase rate from master pricing
        rate = get_purchase_rate(sku, asin)

        # Pack size in kg, so the invoice screen can total the shipment weight instead
        # of the owner reaching for a calculator. 0.0 when unknown — never a guess.
        pack_weight = get_pack_weight(sku, asin)

        items.append({
            "sku": sku,
            "title": title,
            "short_title": " ".join(title.split()[:10]),
            "asin": asin,
            "fnsku": fnsku,
            "quantity": quantity,
            "hsn_code": hsn_info["hsn_code"],
            "gst_rate": hsn_info["gst_rate"],
            "rate": rate,
            "unit": "Pcs",
            "weight": pack_weight,
            "line_weight": round(quantity * pack_weight, 3),
        })

    # The same calculation the Shipment tab uses, from the same function, so an invoice
    # raised from a CSV and one raised from a plan cannot disagree about the weight.
    from app.shipment.logic import shipment_weight

    return {
        "metadata": metadata,
        "items": items,
        "weight": shipment_weight(items),
    }
