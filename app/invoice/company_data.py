"""
F2D Tech company data: GSTINs by state, supplier info, invoice numbering.
"""

SUPPLIER_NAME = "F2D TECH PRIVATE LIMITED"
SUPPLIER_ADDRESS = "C/O Dinesh Prasad Sah, New Babu Para, Near Dadi Shyam Mandir, Dumka, Jharkhand 814101"
SUPPLIER_STATE = "Jharkhand"
SUPPLIER_GSTIN = "20AAFCF9848M1Z7"  # Jharkhand (primary/ship-from)
SUPPLIER_PHONE = "7870034414"

# All F2D Tech GSTINs by state (extracted from Seller Central)
GSTINS_BY_STATE = {
    "Assam": "18AAFCF9848M1ZS",
    "Bihar": "10AAFCF9848M1Z8",
    "Delhi": "07AAFCF9848M1ZV",
    "Gujarat": "24AAFCF9848M1ZZ",
    "Haryana": "06AAFCF9848M1ZX",
    "Jharkhand": "20AAFCF9848M1Z7",
    "Karnataka": "29AAFCF9848M1ZP",
    "Maharashtra": "27AAFCF9848M1ZT",
    "Odisha": "21AAFCF9848M1Z5",
    "Punjab": "03AAFCF9848M1Z3",
    "Rajasthan": "08AAFCF9848M1ZT",
    "Tamil Nadu": "33AAFCF9848M1Z0",
    "Telangana": "36AAFCF9848M1ZU",
    "Uttar Pradesh": "09AAFCF9848M1ZR",
    "West Bengal": "19AAFCF9848M1ZQ",
}

# State code (first 2 digits of GSTIN) to state name mapping
STATE_CODE_TO_NAME = {
    "01": "Jammu & Kashmir", "02": "Himachal Pradesh", "03": "Punjab",
    "04": "Chandigarh", "05": "Uttarakhand", "06": "Haryana",
    "07": "Delhi", "08": "Rajasthan", "09": "Uttar Pradesh",
    "10": "Bihar", "11": "Sikkim", "12": "Arunachal Pradesh",
    "13": "Nagaland", "14": "Manipur", "15": "Mizoram",
    "16": "Tripura", "17": "Meghalaya", "18": "Assam",
    "19": "West Bengal", "20": "Jharkhand", "21": "Odisha",
    "22": "Chhattisgarh", "23": "Madhya Pradesh", "24": "Gujarat",
    "25": "Daman & Diu", "26": "Dadra & Nagar Haveli",
    "27": "Maharashtra", "28": "Andhra Pradesh", "29": "Karnataka",
    "30": "Goa", "31": "Lakshadweep", "32": "Kerala",
    "33": "Tamil Nadu", "34": "Puducherry", "35": "Andaman & Nicobar",
    "36": "Telangana", "37": "Andhra Pradesh (New)",
}

# Default transporters
TRANSPORTERS = [
    "All Cargo Logistics",
    "VRL Logistics",
]

# Priority FC addresses — exact shipping addresses used most often
# These override the generic FC address file
PRIORITY_FC_ADDRESSES = {
    "BLR4": {
        "name": "Amazon Seller Services Private Limited",
        "address": "Plot No. 12 P2, Hitech, Defence and Aerospace Park, Devanahalli, BENGALURU, KARNATAKA 562149, IN",
        "state": "Karnataka",
        "pincode": "562149",
    },
    "DED3": {
        "name": "ASSPL - Haryana",
        "address": "Block J2, Farukhnagar Logistics Parks, LLP, Village- Farrukhnagar, Tehsil- Farrukhanagar, Gurgaon, HARYANA 122506, IN",
        "state": "Haryana",
        "pincode": "122506",
    },
    "ISK3": {
        "name": "Amazon Seller Services Private Limited",
        "address": "Royal Warehousing and Logistics LLP, Survey Number 45, Hissa No.4A, Village Pise Village, Aamne Post, BHIWANDI, MAHARASHTRA 421302, IN",
        "state": "Maharashtra",
        "pincode": "421302",
    },
}


def get_gstin_for_state(state: str) -> str:
    """Get F2D Tech's GSTIN for a given state."""
    state = state.strip().title()
    # Direct match
    if state in GSTINS_BY_STATE:
        return GSTINS_BY_STATE[state]
    # Partial match
    for s, gstin in GSTINS_BY_STATE.items():
        if state.lower() in s.lower() or s.lower() in state.lower():
            return gstin
    return ""


def get_next_invoice_number(last_number: int) -> str:
    """
    Generate next invoice number in format ST/YY-YY/NNN
    e.g. ST/26-27/028 (financial year April 2026 to March 2027)
    """
    from datetime import datetime
    now = datetime.now()
    # Indian financial year: April to March
    if now.month >= 4:
        fy_start = now.year % 100
        fy_end = (now.year + 1) % 100
    else:
        fy_start = (now.year - 1) % 100
        fy_end = now.year % 100

    next_num = last_number + 1
    return f"ST/{fy_start:02d}-{fy_end:02d}/{next_num:03d}"
