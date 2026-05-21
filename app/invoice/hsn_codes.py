"""
HSN Code management for FBA invoices.
Based on CBIC GST HSN classification (verified from https://services.gst.gov.in/services/searchhsnsac)

Default: HSN 1106 at 5% GST for all F2D processed food products.
This is consistent with existing invoice practice (stock transfer of packaged food).

HSN 1106: Flour, meal, powder of dried leguminous vegetables, of products of Chapter 8
          Covers: Sattu, Makhana, Roasted Chana, Besan, Moringa powder, Thekua, etc.

The master database (hsn_master.json) stores verified HSN codes by SKU/product.
Once verified, codes are never looked up again.
"""
import json
from pathlib import Path
from typing import Optional

BASE_DIR = Path(__file__).parent
HSN_MASTER_FILE = BASE_DIR / "hsn_master.json"

# Default HSN for all F2D food products (verified from existing invoices)
DEFAULT_HSN = "1106"
DEFAULT_GST_RATE = 5


def load_hsn_master() -> dict:
    """Load the verified HSN master database."""
    if HSN_MASTER_FILE.exists():
        with open(HSN_MASTER_FILE, "r") as f:
            return json.load(f)
    return {}


def save_hsn_master(master: dict):
    """Save updated HSN master database."""
    with open(HSN_MASTER_FILE, "w") as f:
        json.dump(master, f, indent=2)


def update_hsn_for_sku(sku: str, hsn_code: str, gst_rate: int = 5):
    """Update/save HSN code for a specific SKU in master database."""
    master = load_hsn_master()
    master[sku] = {"hsn_code": hsn_code, "gst_rate": gst_rate}
    save_hsn_master(master)


def lookup_hsn(title: str, sku: str = "") -> dict:
    """
    Look up HSN code. Priority:
    1. HSN master database (previously verified/saved codes)
    2. Default 1106 at 5% (consistent with existing invoice practice)

    All F2D products are processed food at 5% GST.
    HSN 1106 covers flour/meal/powder of legumes, cereals, and Ch.8 products.
    """
    # Check master database first
    master = load_hsn_master()
    if sku and sku in master:
        entry = master[sku]
        return {
            "hsn_code": entry["hsn_code"],
            "gst_rate": entry.get("gst_rate", DEFAULT_GST_RATE),
            "description": "From verified master",
        }

    # Default: 1106 at 5% for all food products (per existing invoice practice)
    return {
        "hsn_code": DEFAULT_HSN,
        "gst_rate": DEFAULT_GST_RATE,
        "description": "Default (flour/meal/powder of legumes & cereals)",
    }


def save_invoice_hsn_codes(items: list):
    """
    After an invoice is finalized, save all HSN codes to master database.
    This way they don't need to be looked up again.
    """
    master = load_hsn_master()
    updated = False
    for item in items:
        sku = item.get("sku", "")
        hsn = item.get("hsn_code", DEFAULT_HSN)
        gst = item.get("gst_rate", DEFAULT_GST_RATE)
        if sku and (sku not in master or master[sku]["hsn_code"] != hsn):
            master[sku] = {"hsn_code": hsn, "gst_rate": gst}
            updated = True
    if updated:
        save_hsn_master(master)
