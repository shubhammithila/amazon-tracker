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


FC_ADDRESSES = load_fc_addresses()
PRICING = load_pricing()


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
        })

    return {"metadata": metadata, "items": items}
