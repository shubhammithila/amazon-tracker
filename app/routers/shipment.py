"""Shipment Maker — CSV upload to packing plan, daily packing entry, documents.

Storage moved out of ``shipment_plan.json`` and into the database. The reason is
not tidiness: two roles now write concurrently. The owner edits plan quantities
while the operations employee records what was packed, and the old
whole-file-overwrite ``POST /save`` meant whoever saved last silently destroyed
the other's work. Plan rows and packing rows are now separate records with
separate endpoints and separate roles, so there is nothing to clobber.

Auth is per-route rather than router-wide, because this is the one router where
the distinction matters:

    admin only     generate, edit items, thresholds, delete, verify, release
    ops + admin    read the plan, enter packing, submit a day, morning PDF

``parse_sales_csv`` and ``parse_stock_csv`` are unchanged — they work, and
rewriting a working parser during a storage migration would make a failure
impossible to attribute.
"""
import io
import json
import logging
import re
from datetime import date, datetime
from pathlib import Path

import pandas as pd
from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_db
from app.invoice.company_data import SUPPLIER_GSTIN
from app.invoice.hsn_codes import lookup_hsn
from app.invoice.parser import get_fc_info, get_purchase_rate
from app.models import Invoice
from app.routers.auth import ROLE_ADMIN, require_admin, require_ops_or_admin
from app import products
from app.shipment import catalogue, documents, logic, repository

router = APIRouter(prefix="/shipment")
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent.parent
FAMILIES_FILE = BASE_DIR / "invoice" / "product_families.json"
LEGACY_DATA_FILE = BASE_DIR.parent / "shipment_plan.json"


def load_families() -> dict:
    if FAMILIES_FILE.exists():
        with open(FAMILIES_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


FAMILIES = load_families()


def load_fc_choices() -> list[dict]:
    """Every FC we can resolve, for the destination picker.

    Built from the same two sources `get_fc_info` reads — the 93-address file and the
    three priority addresses — so the picker cannot offer a code that then fails to
    resolve on the invoice. Derived once at import: the file does not change at runtime.

    Each entry carries whether we hold a GSTIN for its state, because that is the
    difference between an FC we can legally ship to and one we cannot. There are FCs in
    Madhya Pradesh, Kerala and Andhra Pradesh where we have no registration, and India
    requires the destination FC to be an Additional Place of Business on a GST
    registration IN that state. Sorted with the usable ones first, then by code.
    """
    from app.invoice.company_data import PRIORITY_FC_ADDRESSES, get_gstin_for_state
    from app.invoice.parser import FC_ADDRESSES

    codes = set(FC_ADDRESSES) | set(PRIORITY_FC_ADDRESSES)
    choices = []
    for code in codes:
        info = PRIORITY_FC_ADDRESSES.get(code) or FC_ADDRESSES.get(code) or {}
        state = (info.get("state") or "").strip()
        choices.append({
            "code": str(code).upper(),
            "state": state,
            "gstin": get_gstin_for_state(state) if state else "",
            "priority": code in PRIORITY_FC_ADDRESSES,
        })
    choices.sort(key=lambda c: (not c["priority"], not c["gstin"], c["code"]))
    return choices


FC_CHOICES = load_fc_choices()
KNOWN_FC_CODES = frozenset(c["code"] for c in FC_CHOICES)


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


def parse_sku_map(content: bytes) -> dict[str, str]:
    """{ASIN: merchant SKU} from the stock report.

    Split out of generate_plan and no longer wrapped in a bare
    `except Exception: pass`. Amazon's shipment upload keys on the merchant SKU,
    so an empty fba_sku means that row is rejected on their side — swallowing the
    parse error made a real failure invisible. Now it is logged, and
    /shipment/active reports how many items are missing a SKU.
    """
    try:
        df = pd.read_csv(io.BytesIO(content))
    except Exception:
        try:
            df = pd.read_csv(io.BytesIO(content), encoding="utf-8-sig")
        except Exception:
            logger.warning("Could not read the stock CSV for SKU mapping", exc_info=True)
            return {}

    if "sku" not in df.columns or "asin" not in df.columns:
        logger.warning(
            "Stock CSV has no 'sku'/'asin' pair (columns: %s) — plan items will "
            "have no merchant SKU and Amazon will reject the upload.",
            list(df.columns)[:12],
        )
        return {}

    out: dict[str, str] = {}
    for asin, sku in zip(df["asin"].astype(str), df["sku"].astype(str)):
        asin = asin.strip()
        sku = sku.strip()
        if asin and sku and sku.lower() != "nan":
            out.setdefault(asin, sku)
    return out


def _item_payload(item, packed: int, shippable: int) -> dict:
    """One plan item as the frontend consumes it.

    `packed` and `shippable` are both sent, deliberately. Held units are packed
    (they are in boxes; re-packing them would double the order) but not shippable
    (the day is parked). Collapsing them into one number is the subtle bug this
    whole feature is built to avoid.
    """
    planned = int(item.shipment_plan or 0)
    available = int(item.available or 0)
    return {
        "asin": item.asin,
        "fba_sku": item.fba_sku or "",
        "brand": item.brand or "",
        "item": item.item or "",
        "weight": float(item.weight or 0),
        # The label is computed server-side and sent, rather than each template
        # formatting the raw number itself. Three renderers were each doing it
        # differently ("0.5kg", "0.5 kg", "500 g"), and a rule reimplemented in
        # JavaScript is a rule that drifts from the printed sheet.
        "weight_label": logic.weight_label(item.weight),
        "sales_7d": int(item.sales_7d or 0),
        "projection": int(item.projection or 0),
        "fba_stock": int(item.fba_stock or 0),
        "deficit": int(item.deficit or 0),
        "shipment_plan": planned,
        "available": available,
        "s": bool(item.s),
        "m": bool(item.m),
        "b": bool(item.b),
        "packed": packed,
        "shippable": shippable,
        # Two different questions, so two numbers. `remaining` is what still has
        # to be BOXED and ignores warehouse stock, because stock on a shelf is
        # not in a carton yet. `to_source` is what still has to be MADE, and is
        # the one that reacts when the owner types into the In-stock column.
        "remaining": logic.remaining_for(planned, packed),
        "to_source": logic.still_to_source(planned, packed, available),
        # Units boxed beyond the plan. Sent as its own number because `remaining`
        # clamps at 0, so an over-pack is otherwise indistinguishable from a row
        # that is exactly finished — and the invoice bills what was PACKED, not
        # what was planned.
        "over_packed": logic.over_packed(planned, packed),
        # Only ever non-null on the owner's draft view, which is the single caller
        # that asks for excluded rows. Everywhere else they are filtered out in
        # SQL, so this is None and the frontend renders nothing special.
        "excluded_at": item.excluded_at.isoformat() if item.excluded_at else None,
        # The joined sort priority, so the screen can show WHY a row sits where it
        # does. Attached by load_plan_items; defaulted for rows loaded elsewhere.
        "category": int(
            getattr(item, "category_rank", None) or logic.DEFAULT_CATEGORY
        ),
    }


async def _invoice_numbers(db, invoice_ids) -> dict:
    """{invoice row id: "ST/26-27/046"} for the ids given.

    Every message that names an invoice goes through this. The days store an
    `invoice_id`, which is a database row id — "already on invoice #5" names something
    the owner has never seen and cannot search for, while the document in his hand says
    "ST/26-27/032". Three separate messages were interpolating the raw id, and one of
    them had a comment claiming it printed the real number.

    One query for the whole set rather than a lookup per day, and missing ids simply do
    not appear, so a caller falling back to `#id` still works when an invoice row is
    gone.
    """
    ids = {int(i) for i in invoice_ids if i}
    if not ids:
        return {}
    rows = await db.execute(
        select(Invoice.id, Invoice.invoice_no).where(Invoice.id.in_(ids))
    )
    return {row.id: row.invoice_no for row in rows}


def _plan_payload(plan, items, days) -> dict:
    packed_map = logic.packed_units_by_asin(days)
    shippable_map = logic.shippable_units_by_asin(days)
    return {
        "plan": {
            "id": plan.id,
            "label": plan.label,
            "multiplier": float(plan.multiplier or 5),
            "status": plan.status,
            "min_cartons": int(plan.min_cartons or 0),
            "min_units": int(plan.min_units or 0),
            "created_at": plan.created_at.isoformat() if plan.created_at else None,
        },
        "items": [
            _item_payload(i, packed_map.get(i.asin, 0), shippable_map.get(i.asin, 0))
            for i in items
        ],
        "days": days,
        "held": logic.held_totals(days),
        # The other half of requirement 9. `held` says what is parked; this says
        # whether the parked days have now added up to a shipment. Without it the
        # owner has to add the held columns himself every morning, and held stock
        # sits until someone happens to notice.
        "carry_over": logic.carry_over(days, plan.min_cartons, plan.min_units),
    }


# ─── Plan lifecycle (admin) ──────────────────────────────────────────────────

@router.post("/generate")
async def generate_plan(
    request: Request,
    sales_csv: UploadFile = File(...),
    stock_csv: UploadFile = File(...),
    multiplier: float = Form(5.0),
    db: AsyncSession = Depends(get_db),
    role: str = Depends(require_admin),
):
    """Upload sales + stock CSVs → a new active plan, closing the previous one."""
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

    sku_map = parse_sku_map(stock_content)

    # ── The product list comes from the MRP sheet, live ──────────────────────
    #
    # **The sheet decides which products exist**, not merely which are still sold.
    # That distinction is the bug this fixes: Triphala Sattu was in the sheet in two
    # pack sizes, marked Active, and never appeared in a plan — because this loop used
    # to iterate FAMILIES, the static product_families.json, which had 205 ASINs and
    # had never had Triphala added to it. The sheet was consulted only for a yes/no
    # flag, so a genuinely new product was invisible whatever the sheet said.
    #
    # Never raises: on a fetch failure this returns the cached copy (or nothing) plus
    # a warning, because a Google outage must not stop the owner building a plan.
    sheet_products, sheet_warning, sheet_source = await catalogue.load_catalogue()

    # The union, so neither source can lose a product on its own:
    #   * in the sheet only  -> new product, include it (this is Triphala)
    #   * in both            -> sheet wins on name/weight/brand/active
    #   * in the file only   -> the sheet has never heard of it. Keep it: absence is
    #                           missing information, not a decision to discontinue,
    #                           and dropping it would shrink the plan silently.
    #                           (Currently empty — the sheet is a strict superset.)
    candidates = set(FAMILIES) | set(sheet_products)

    items: list[dict] = []
    skipped_inactive = 0
    from_sheet_only = []

    for asin in sorted(candidates):
        info = FAMILIES.get(asin) or {}
        sheet_row = sheet_products.get(asin) or {}

        # is_active() treats an unknown ASIN as active, which is what keeps a
        # file-only product in the plan rather than silently dropping it.
        if sheet_row and not sheet_row.get("active"):
            skipped_inactive += 1
            continue
        if not sheet_row and not catalogue.is_active(
            {a: bool(r["active"]) for a, r in sheet_products.items()}, asin
        ):
            skipped_inactive += 1
            continue

        # Sheet first for every displayed field, then the static file, then a default.
        # The sheet is hand-maintained and current; the file is a months-old snapshot.
        # Verified before switching: for the 108 active ASINs in both, weight and brand
        # agree exactly — 0 mismatches — so this changes no existing row.
        brand_name = sheet_row.get("brand") or info.get("brand") or ""
        product_name = sheet_row.get("name") or info.get("parent_product") or ""
        weight = sheet_row.get("weight")
        if weight is None:
            weight = info.get("weight") or 0

        if sheet_row and asin not in FAMILIES:
            from_sheet_only.append(f"{product_name} {logic.weight_label(weight)}".strip())

        units_sold = sales.get(asin, 0)
        fba_stock = stock.get(asin, 0)
        projection = int(units_sold * multiplier)
        deficit = projection - fba_stock

        items.append({
            # Substring, not equality: the sheet says "Mithila Foods" while the static
            # file's values are compared exactly. Anything not Mithila is Howrah, which
            # matches the sheet's only two brand values.
            "brand": "MF" if "mithila" in brand_name.lower() else "HF",
            # NOT from the sheet. Column M ("Amazon FBA SKU") is blank on all 108 active
            # rows, and the real value arrives in the uploaded stock CSV — Amazon's own
            # export. Reading the sheet's empty column would blank the SKU on every row,
            # and Amazon rejects those lines.
            "fba_sku": sku_map.get(asin, ""),
            "asin": asin,
            "item": product_name,
            "weight": weight,
            "sales_7d": units_sold,
            "projection": projection,
            "fba_stock": fba_stock,
            "deficit": deficit,
            # Rounding happens HERE and nowhere else, then is persisted. Doing it
            # in a renderer would mean the screen and the download could disagree,
            # and a manual override would be silently re-rounded on every view.
            "shipment_plan": logic.round_to_10(max(0, deficit)),
            "available": 0,
            "s": False, "m": False, "b": False,
        })

    # Check for stock parked on the plan about to be closed, BEFORE creating the
    # new one. /active only ever shows the active plan, so a day held on Saturday
    # silently drops off every screen when Monday's plan is generated — the boxes
    # are still in the warehouse and nothing in the app mentions them again. The
    # generate is not blocked (the owner may well have shipped them and simply
    # not marked it), but he is told.
    outgoing = await repository.get_active_plan(db)
    abandoned_holds = (
        [
            {
                "pack_date": d.pack_date,
                "units": int(d.total_units or 0),
                "cartons": int(d.total_cartons or 0),
            }
            for d in await repository.load_held_days(db, outgoing.id)
        ]
        if outgoing is not None
        else []
    )

    # What the previous plan contained, read BEFORE the new draft is created, so the
    # response can say which products appeared and which vanished. The sheet is
    # hand-edited: a stale Active flag shows up here as a row silently leaving the
    # plan, and "110 rows" alone gives the owner no way to notice.
    previous_asins: set[str] = set()
    if outgoing is not None:
        previous_asins = {
            i.asin for i in await repository.load_plan_items(db, outgoing.id)
        }

    # A DRAFT, and the active plan is left completely alone. The owner removes
    # rows, fixes quantities and fills missing SKUs before the warehouse ever
    # sees it; POST /plan/{id}/finalise is what promotes it. Any previous EMPTY draft is
    # discarded first so there is only ever one to work on.
    #
    # A draft holding packing days is NOT discarded — it is kept and its days moved onto
    # the plan being created. Deleting it destroyed 400 packed units in 9 cartons on
    # production: closing a plan carries unshipped days onto a carrier draft, and the next
    # CSV upload then deleted that draft with `days` cascading delete-orphan.
    _deleted, carrier_ids = await repository.delete_draft_plans(db)
    plan = await repository.create_plan(
        db, items, multiplier=multiplier, status=repository.STATUS_DRAFT
    )

    # Move the carried days onto the new draft, then retire the emptied carrier. Rows for
    # any ASIN the new CSV does not list are inserted first, at To Ship 0, for the same
    # reason close_plan does it: packed units with no plan row reach no GST invoice line.
    rescued: list[str] = []
    for carrier_id in carrier_ids:
        carried_days = await repository.load_days_with_entries(db, carrier_id)
        dates = [d["pack_date"] for d in carried_days]
        if not dates:
            continue
        asins = sorted({
            e["asin"] for d in carried_days for e in (d.get("entries") or [])
            if e.get("asin")
        })
        await repository.ensure_rows_for_asins(
            db, plan.id, asins, source_plan_id=carrier_id
        )
        rescued += await repository.carry_days_to_plan(db, carrier_id, plan.id, dates)
        # Empty now, so the original reason for deleting a stale draft applies again.
        await repository.delete_draft_plans(db)

    stored = await repository.load_plan_items(db, plan.id)
    missing_sku = await repository.count_items_missing_sku(db, plan.id)

    days = await repository.load_days_with_entries(db, plan.id)
    payload = _plan_payload(plan, stored, days)
    payload["carried_days"] = sorted(rescued)
    payload["missing_sku_count"] = missing_sku
    payload["abandoned_holds"] = abandoned_holds
    payload["skipped_inactive"] = skipped_inactive

    # Where the product list came from, and what moved. The owner asked for the sheet
    # to drive the plan; this is how he can see that it did, and check the sheet before
    # a wrong Active flag reaches a real shipment.
    current_asins = {i["asin"] for i in items}
    added = sorted(current_asins - previous_asins) if previous_asins else []
    removed = sorted(previous_asins - current_asins) if previous_asins else []

    def _label(asin: str) -> str:
        row = sheet_products.get(asin) or {}
        info = FAMILIES.get(asin) or {}
        name = row.get("name") or info.get("parent_product") or asin
        weight = row.get("weight")
        if weight is None:
            weight = info.get("weight") or 0
        return f"{name} {logic.weight_label(weight)}".strip()

    payload["catalogue"] = {
        "source": sheet_source,             # "sheet" | "cache" | "none"
        "sheet_products": len(sheet_products),
        "active": sum(1 for r in sheet_products.values() if r["active"]),
        "skipped_inactive": skipped_inactive,
        "rows": len(items),
        # Named, not just counted: "2 new products" sends the owner hunting through
        # 110 rows to find out which.
        "added": [_label(a) for a in added],
        "removed": [_label(a) for a in removed],
        "new_to_the_catalogue": sorted(from_sheet_only),
    }

    warnings = []
    # First, because it explains the row count. Without it the owner sees 110 rows
    # where he expected 205 and has no way to know why.
    if sheet_warning:
        warnings.append(sheet_warning)
    if missing_sku:
        warnings.append(
            f"{missing_sku} item(s) to ship have no merchant SKU. Amazon's upload "
            "needs the SKU, not the ASIN, so those rows will be rejected."
        )
    if abandoned_holds:
        parked_units = sum(h["units"] for h in abandoned_holds)
        parked_cartons = sum(h["cartons"] for h in abandoned_holds)
        warnings.append(
            f"The previous plan had {len(abandoned_holds)} held day(s) carrying "
            f"{parked_units} units in {parked_cartons} cartons "
            f"({', '.join(h['pack_date'] for h in abandoned_holds)}). Those boxes "
            "are packed but were never shipped, and this new plan does not know "
            "about them — ship or write them off before packing against it."
        )
    if rescued:
        # Said out loud, because the alternative is what happened on production: the days
        # were silently deleted and the owner found out by not finding them.
        carried_units = sum(
            d["total_units"] for d in days if d["pack_date"] in rescued
        )
        warnings.append(
            f"{len(rescued)} packed day(s) carried over from the plan you closed "
            f"({', '.join(rescued)}, {carried_units} units) are on this new plan, so "
            "they are counted as already packed and will not be packed again."
        )
    if warnings:
        payload["warning"] = " ".join(warnings)
    return JSONResponse(payload)


#: The ship-from address, as Amazon accepts it. Read back from a real inbound plan on the
#: live account rather than assembled from SUPPLIER_ADDRESS, because Amazon validated
#: THIS exact shape — a hand-built variant risks a rejection at the one moment it matters.
async def product_prices_for(db, line: dict) -> float:
    """The purchase rate for one Amazon plan line, in rupees per unit.

    A named helper rather than an inline call because both the missing-rate guard and the
    compliance loop need the SAME number: if they disagreed, the guard would pass a
    product whose declared value then went to Amazon as 0 — which is rejected with "We
    encountered an internal error", a message that looks like a fault on their side.

    `_asin` is the display-only field the plan-body builder carries; the msku is the
    fallback, for a rate that exists in `pricing_data.json` under a SKU key only.
    """
    return await products.rate_for(db, line.get("_asin", ""), line.get("msku", ""))


AMAZON_SOURCE_ADDRESS = {
    "name": "F2D TECH PRIVATE LIMITED, MITHILA FOODS",
    "companyName": "F2D TECH PRIVATE LIMITED, MITHILA FOODS",
    "addressLine1": "C/O DINESH PRASAD SAH, new babu para,near dadi shyam mandir",
    "addressLine2": "Dumka jharkhand",
    "city": "Dumka",
    "stateOrProvinceCode": "Jharkhand",
    "postalCode": "814101",
    "countryCode": "IN",
    "phoneNumber": "7870034414",
    "email": "f2dtechpvtltd@gmail.com",
}


async def _verified_days_for_amazon(request: Request, db: AsyncSession):
    """Validate a `{"pack_dates": [...]}` body and build the Amazon plan body from it.

    Shared by the dry run and the real creation, because the two must agree completely:
    if the preview validated differently from the create, the owner would approve one thing
    and send another. One function means "what the preview showed" and "what gets sent" are
    the same code path.

    Returns ``((plan, pack_dates, days, preview), None)`` or ``(None, JSONResponse)``.
    """
    body, error = await _json_object(request)
    if error:
        return None, error

    raw_dates = body.get("pack_dates") or []
    if isinstance(raw_dates, str):
        raw_dates = [raw_dates]
    pack_dates: list[str] = []
    for value in raw_dates:
        text = str(value).strip()
        if text and text not in pack_dates:
            pack_dates.append(text)
    if not pack_dates:
        return None, JSONResponse(
            {"error": "Select at least one packed day."}, status_code=400
        )
    invalid = [d for d in pack_dates if not _valid_date(d)]
    if invalid:
        return None, JSONResponse(
            {"error": f"Not a date (YYYY-MM-DD): {', '.join(invalid)}"}, status_code=400
        )

    plan = await repository.get_active_plan(db)
    if plan is None:
        return None, _no_plan()

    days = [
        d for d in await repository.load_days_with_entries(db, plan.id)
        if d["pack_date"] in set(pack_dates)
    ]
    missing_days = set(pack_dates) - {d["pack_date"] for d in days}
    if missing_days:
        return None, JSONResponse(
            {"error": f"Nothing was packed on {', '.join(sorted(missing_days))}."},
            status_code=404,
        )

    # Verified only, for the same reason the invoice bridge insists on it: this creates
    # the shipment those boxes travel in, and unapproved counts must not reach Amazon.
    unverified = [d for d in days if d["status"] not in logic.INVOICEABLE_STATUSES]
    if unverified:
        return None, JSONResponse(
            {
                "error": "These days are not verified yet: "
                + ", ".join(f"{d['pack_date']} ({d['status']})" for d in unverified)
                + ". Verify them before creating the shipment.",
            },
            status_code=400,
        )

    items = await repository.load_plan_items(db, plan.id)
    preview = logic.amazon_plan_body(
        AMAZON_SOURCE_ADDRESS,
        items,
        logic.units_by_asin(days),
        get_settings().sp_api_marketplace_id,
    )
    return (plan, pack_dates, days, preview), None


@router.post("/amazon-shipment-preview")
async def preview_amazon_shipment(
    request: Request,
    db: AsyncSession = Depends(get_db),
    role: str = Depends(require_admin),
):
    """What WOULD be sent to Amazon for the chosen days. Sends nothing.

    Body: ``{"pack_dates": ["2026-08-14"]}``.

    A dry run, and the deliberate step before anything irreversible exists. Creating an
    inbound plan is a real shipment at Amazon that no failed transaction can undo, so the
    owner sees the exact lines and quantities first — with product names, not just mskus,
    because a screen of SKU codes is not something a human can check.

    **Refuses when any line has no merchant SKU.** Asked for directly: "for the ones with
    no sku. warn the user and ask them to fill it , then only the shipment will be
    created." Amazon keys every line on the msku, so such a line is either rejected or
    accepted with the line missing — real cartons arriving at an FC against a shipment
    that does not mention them. Blocking is cheaper than reconciling.

    Quantities are what was PACKED on those days, never the plan: a shipment describes
    boxes that exist.
    """
    loaded, error = await _verified_days_for_amazon(request, db)
    if error:
        return error
    plan, pack_dates, days, preview = loaded

    blockers = []
    if preview["missing_sku"]:
        names = ", ".join(
            f"{m['item']} {m['pack_size']}".strip() for m in preview["missing_sku"][:6]
        )
        extra = len(preview["missing_sku"]) - 6
        blockers.append(
            f"{len(preview['missing_sku'])} product(s) have no merchant SKU: {names}"
            + (f" and {extra} more" if extra > 0 else "")
            + ". Amazon's shipment keys on the merchant SKU, so these boxes would ship "
            "against a shipment that does not list them. Fill the SKU in on the plan "
            "(the Merchant SKU column is editable), then try again."
        )
    elif not preview["lines"]:
        blockers.append("No units were packed on the selected day(s).")

    return JSONResponse({
        "configured": get_settings().spapi_configured,
        "ok": preview["ok"],
        "pack_dates": pack_dates,
        "units": preview["units"],
        "lines": preview["lines"],
        "missing_sku": preview["missing_sku"],
        "blockers": blockers,
        # The literal request, so the owner (or I) can see exactly what would go.
        "request_body": preview["body"],
    })


# ─── Creating the shipment at Amazon ─────────────────────────────────────────
#
# Asked for: "when the packing is done. I want to select the days, select fc and create
# shipment from my app only on amazon using amazon api ... and autofill the shipment id
# and create invoice, download box labels and invoice from my app only."
#
# **Two steps, deliberately.** `create` makes an inbound plan and asks Amazon to place it
# at the chosen FC; `confirm` is what turns that into a real shipment. Splitting them is
# not ceremony — confirmation cannot be undone, so the owner sees the destination Amazon
# offered and the fee it will charge BEFORE the irreversible click. The same reasoning as
# never letting a bookkeeping bug roll back a committed GST invoice number.
#
# India needs no packing information and no carton dimensions: `ListPackingOptions` and
# `ListShipmentBoxes` are both refused for this marketplace, and a real test plan generated
# placement options without them. Cartons are still recorded per DAY for the invoice's Boxes
# field — they are just not something Amazon wants here.


@router.post("/amazon-shipment/create")
async def create_amazon_shipment(
    request: Request,
    db: AsyncSession = Depends(get_db),
    role: str = Depends(require_admin),
):
    """Create an inbound plan at Amazon and place it at the chosen FC. **Stops before
    confirming**, so nothing has shipped yet and the plan can still be cancelled.

    Body: ``{"pack_dates": ["2026-08-14"], "fc_code": "ISK3"}``.

    Refuses when any line has no merchant SKU — Amazon keys on the msku, so such a line is
    either rejected or accepted with the line missing, which means real cartons arriving
    against a shipment that does not list them.

    The response carries the placement options with their FEES and expiry. Both are shown
    before the confirm step: the fee was ₹0 on the test plan but it is Amazon's to change,
    and an expired option cannot be confirmed.
    """
    from app.shipment import spapi

    # Read the body once, then re-validate through the SAME helper the dry run uses, so
    # what was previewed is what gets sent.
    body, error = await _json_object(request)
    if error:
        return error
    fc_code = str(body.get("fc_code") or "").strip().upper()
    if not fc_code:
        return JSONResponse(
            {"error": "Choose the destination FC. It decides where the boxes go and "
                      "which GSTIN the invoice uses."},
            status_code=400,
        )
    if fc_code not in KNOWN_FC_CODES:
        return JSONResponse(
            {"error": f"{fc_code} is not an FC code we know."}, status_code=400
        )


    loaded, error = await _verified_days_for_amazon(request, db)
    if error:
        return error
    plan, pack_dates, days, preview = loaded

    if not preview["ok"]:
        names = ", ".join(
            f"{m['item']} {m['pack_size']}".strip() for m in preview["missing_sku"][:6]
        )
        if preview["missing_sku"]:
            return JSONResponse(
                {
                    "error": f"{len(preview['missing_sku'])} product(s) have no merchant "
                             f"SKU: {names}. Amazon's shipment keys on the merchant SKU, "
                             "so these boxes would ship against a shipment that does not "
                             "list them. Fill the SKU in on the plan, then try again.",
                    "missing_sku": preview["missing_sku"],
                },
                status_code=400,
            )
        return JSONResponse(
            {"error": "No units were packed on the selected day(s)."}, status_code=400
        )

    # Refuse days that already have a shipment. Creating a second one for the same boxes
    # would send Amazon two shipments for one set of cartons, and the FC would receive
    # half of each — the same class of mistake as invoicing the same day twice.
    already = [d["pack_date"] for d in days if d.get("shipment_confirmation_id")]
    if already:
        return JSONResponse(
            {
                "error": "Already sent to Amazon: "
                + ", ".join(
                    f"{d['pack_date']} ({d.get('shipment_confirmation_id')})"
                    for d in days if d.get("shipment_confirmation_id")
                )
                + ". Creating a second shipment for the same boxes would have the FC "
                "expecting twice what is on the truck.",
                "pack_dates": already,
            },
            status_code=409,
        )

    # Credentials last of the cheap checks. Every guard above is about OUR data — an FC
    # typo, an unverified day, a missing SKU, boxes already sent — and each is actionable
    # whether or not Amazon is configured. Reporting "API not set up" first would hide a
    # duplicate shipment behind a credentials message.
    settings = get_settings()
    if not settings.spapi_configured:
        return JSONResponse(
            {"error": "Amazon API is not set up. Add the SP_API_* keys to .env."},
            status_code=400,
        )

    # The declared value is the PER-UNIT purchase rate, the same taxable value the GST
    # invoice uses.
    #
    # **A zero declared value must never be sent.** With no purchase rate on file the
    # route sent `declaredValue: 0`, and Amazon answered "We encountered an internal
    # error. Please try again." — which reads like a transient fault and is not one. The
    # identical call with a real amount succeeded immediately. So a missing rate is
    # reported as the data problem it is, before the plan is even created, rather than
    # producing a misleading error halfway through.
    missing_rate = [
        line for line in preview["lines"]
        if float(await product_prices_for(db, line) or 0) <= 0
    ]
    if missing_rate:
        names = ", ".join(
            f"{line['_item']} {line['_pack_size']}".strip() for line in missing_rate[:6]
        )
        return JSONResponse(
            {
                "error": f"{len(missing_rate)} product(s) have no purchase rate: {names}. "
                "Amazon needs a declared value per SKU for an Indian inbound shipment, "
                "and it must not be zero. Add the rate to the master pricing first.",
                "missing_rate": [
                    {"msku": line["msku"], "item": line["_item"],
                     "pack_size": line["_pack_size"]}
                    for line in missing_rate
                ],
            },
            status_code=400,
        )

    label = f"{plan.label or 'Plan'} · {', '.join(pack_dates)} · {fc_code}"
    try:
        plan_id = await spapi.create_inbound_plan(
            AMAZON_SOURCE_ADDRESS,
            preview["body"]["items"],
            settings.sp_api_marketplace_id,
            name=label[:60],
        )
    except spapi.SpApiError as exc:
        # Amazon's message verbatim: it is what said "does not require prepOwner but
        # SELLER was assigned. Accepted values: [NONE]" and named the exact SKU.
        logger.warning("amazon create plan failed: %s", exc.message)
        return JSONResponse({"error": exc.message}, status_code=502)

    # **Persisted BEFORE placement is confirmed**, and before anything else can fail. A
    # plan confirmed at Amazon with no local record is invisible here and real there, so
    # the id is written the moment it exists.
    await repository.attach_inbound_plan(db, plan.id, pack_dates, plan_id)

    # India requires HSN and a declared value per SKU, and PLACEMENT is where it is
    # enforced: without this the plan creates fine and then placement fails with
    # "ERROR: Declared value need to be provided." Sent for every line rather than only
    # the ones that look unset — Amazon already held these values for the SKUs tested and
    # still refused until they were re-declared. Idempotent, so re-sending is harmless.
    #
    compliance_failures = []
    for line in preview["lines"]:
        rate = await product_prices_for(db, line)
        hsn = await products.tax_for(db, line.get("_asin", ""))
        try:
            await spapi.declare_item_compliance(
                line["msku"],
                hsn_code=hsn["hsn_code"],
                declared_value=float(rate),
                gst_rate=hsn["gst_rate"],
            )
        except spapi.SpApiError as exc:
            compliance_failures.append(f"{line['msku']}: {exc.message}")

    if compliance_failures:
        logger.warning("amazon compliance failed: %s", "; ".join(compliance_failures))
        return JSONResponse(
            {
                "error": "Amazon rejected the GST details for "
                f"{len(compliance_failures)} SKU(s): "
                + "; ".join(compliance_failures[:3]),
                "inbound_plan_id": plan_id,
                "hint": "The plan exists at Amazon but is not placed. Cancel it and fix "
                        "the HSN or purchase rate first.",
            },
            status_code=502,
        )

    try:
        options = await spapi.generate_placement_options(
            plan_id, fc_code, preview["body"]["items"]
        )
    except spapi.SpApiError as exc:
        logger.warning("amazon placement failed for %s: %s", plan_id, exc.message)
        return JSONResponse(
            {
                "error": exc.message,
                "inbound_plan_id": plan_id,
                "hint": "The plan exists at Amazon but has no placement. Cancel it, or "
                        "finish it in Seller Central.",
            },
            status_code=502,
        )

    # The destination Amazon actually chose, per option. `customPlacement` was honoured on
    # the test plan, but this is read back rather than assumed — the FC decides the GSTIN.
    detail = []
    for option in options:
        shipments = []
        for shipment_id in option.get("shipmentIds") or []:
            try:
                shipment = await spapi.get_shipment(plan_id, shipment_id)
                shipments.append(shipment.as_dict())
            except spapi.SpApiError:
                shipments.append({"shipment_id": shipment_id})
        detail.append({
            "placement_option_id": option.get("placementOptionId"),
            "status": option.get("status"),
            "expiration": option.get("expiration"),
            "fees": [
                {
                    "target": f.get("target"),
                    "amount": (f.get("value") or {}).get("amount"),
                    "currency": (f.get("value") or {}).get("code"),
                }
                for f in (option.get("fees") or [])
            ],
            "shipments": shipments,
        })

    return JSONResponse({
        "inbound_plan_id": plan_id,
        "requested_fc": fc_code,
        "pack_dates": pack_dates,
        "units": preview["units"],
        "lines": len(preview["lines"]),
        "placement_options": detail,
        "confirmed": False,
        "next": "Review the destination and fee, then confirm to create the shipment.",
    })


@router.post("/amazon-shipment/confirm")
async def confirm_amazon_shipment(
    request: Request,
    db: AsyncSession = Depends(get_db),
    role: str = Depends(require_admin),
):
    """**THE COMMIT POINT.** Confirm placement — this creates the real shipment.

    Body: ``{"inbound_plan_id": "wf…", "placement_option_id": "pl…",
    "pack_dates": [...]}``.

    After this, `FBA…` ids exist at Amazon, Seller Central shows a working shipment, and
    any placement fee is incurred. Nothing here can undo it.

    The `FBA…` confirmation id and the destination Amazon chose are written onto the packing
    days, which is what lets the invoice fill itself in and the box labels be fetched.
    """
    from app.shipment import spapi

    if not get_settings().spapi_configured:
        return JSONResponse(
            {"error": "Amazon API is not set up."}, status_code=400
        )

    body, error = await _json_object(request)
    if error:
        return error
    plan_id = str(body.get("inbound_plan_id") or "").strip()
    option_id = str(body.get("placement_option_id") or "").strip()
    if not plan_id or not option_id:
        return JSONResponse(
            {"error": "inbound_plan_id and placement_option_id are both required."},
            status_code=400,
        )

    raw_dates = body.get("pack_dates") or []
    if isinstance(raw_dates, str):
        raw_dates = [raw_dates]
    pack_dates = [str(d).strip() for d in raw_dates if str(d).strip()]
    invalid = [d for d in pack_dates if not _valid_date(d)]
    if invalid:
        return JSONResponse(
            {"error": f"Not a date (YYYY-MM-DD): {', '.join(invalid)}"}, status_code=400
        )

    plan = await repository.get_active_plan(db)
    if plan is None:
        return _no_plan()

    try:
        await spapi.confirm_placement(plan_id, option_id)
        shipments = await spapi.plan_shipments(plan_id)
    except spapi.SpApiError as exc:
        logger.warning("amazon confirm failed for %s: %s", plan_id, exc.message)
        return JSONResponse(
            {
                "error": exc.message,
                "inbound_plan_id": plan_id,
                "hint": "Check Seller Central before retrying — if the confirmation went "
                        "through, retrying would not create a second shipment but the app "
                        "would not know the id.",
            },
            status_code=502,
        )

    # Record what Amazon actually created. The destination comes from Amazon, never from
    # the FC that was requested: they can differ, and the destination state decides which
    # of the 15 GSTINs the invoice must use.
    if shipments and pack_dates:
        first = shipments[0]
        await repository.attach_amazon_shipment(
            db, plan.id, pack_dates,
            inbound_plan_id=plan_id,
            amazon_shipment_id=first.shipment_id,
            confirmation_id=first.confirmation_id,
            warehouse_id=first.warehouse_id,
            state=first.state,
        )

    return JSONResponse({
        "confirmed": True,
        "inbound_plan_id": plan_id,
        "shipments": [s.as_dict() for s in shipments],
        # Named so the screen can say it plainly: more than one shipment means Amazon split
        # the plan, and each part needs its own labels.
        "split": len(shipments) > 1,
    })


@router.post("/amazon-shipment/cancel")
async def cancel_amazon_shipment(
    request: Request,
    db: AsyncSession = Depends(get_db),
    role: str = Depends(require_admin),
):
    """Void an inbound plan at Amazon, and forget it locally.

    For a plan created by mistake, or one whose placement failed halfway. Verified against
    the live account: the plan status becomes `VOIDED`.

    Deliberately available: without it, a mis-click leaves a plan sitting in Seller Central
    for ever, and the days would refuse a second attempt because they already carry a plan
    id.
    """
    from app.shipment import spapi

    if not get_settings().spapi_configured:
        return JSONResponse({"error": "Amazon API is not set up."}, status_code=400)

    body, error = await _json_object(request)
    if error:
        return error
    plan_id = str(body.get("inbound_plan_id") or "").strip()
    if not plan_id:
        return JSONResponse({"error": "inbound_plan_id is required."}, status_code=400)

    plan = await repository.get_active_plan(db)
    try:
        await spapi.cancel_inbound_plan(plan_id)
    except spapi.SpApiError as exc:
        return JSONResponse({"error": exc.message}, status_code=502)

    if plan is not None:
        await repository.clear_inbound_plan(db, plan.id, plan_id)
    return JSONResponse({"cancelled": True, "inbound_plan_id": plan_id})


@router.get("/amazon-shipment/labels")
async def amazon_shipment_labels(
    request: Request,
    confirmation_id: str,
    page_type: str = "PackageLabel_Thermal",
    role: str = Depends(require_admin),
):
    """A download URL for Amazon's box labels.

    Returns the URL rather than proxying the PDF: it is a short-lived signed Amazon link,
    and streaming it through this app would add a timeout to a download that works fine
    directly.

    Only the three page types that actually work for a non-partnered ("Other" carrier)
    shipment are offered — A4_2 and Letter_2 are refused by Amazon for this shipment type,
    and an option that always errors reads as a broken app.
    """
    from app.shipment import spapi

    if not get_settings().spapi_configured:
        return JSONResponse({"error": "Amazon API is not set up."}, status_code=400)
    confirmation_id = confirmation_id.strip().upper()
    if not confirmation_id:
        return JSONResponse({"error": "confirmation_id is required."}, status_code=400)

    try:
        url = await spapi.label_url(confirmation_id, page_type)
    except spapi.SpApiError as exc:
        return JSONResponse({"error": exc.message}, status_code=502)
    return JSONResponse({
        "url": url,
        "page_type": page_type,
        "formats": list(spapi.LABEL_PAGE_TYPES),
    })


@router.get("/amazon-shipments")
async def list_amazon_shipments(
    request: Request, role: str = Depends(require_admin)
):
    """Recent Amazon inbound shipments, so the owner picks instead of typing.

    **Read-only.** Nothing is created, confirmed or modified at Amazon. This is the
    lookup that retires the hand-typed shipment ID: each entry carries the `FBA15…`
    confirmation id, the FC Amazon actually chose, and the destination state — which is
    what decides the GSTIN and the inter-state/intra-state GST split.

    The state comes from **Amazon's answer**, not from the FC the owner picked when he
    built the sheet. His pick is a request; this is what happened. They can differ, and a
    tax document has to carry the truth.

    Admin only, matching the plan sheet and the Amazon upload: this is the owner's data
    about his own shipments, and ops has no use for it.

    Answers 200 with `configured: false` rather than an error when there are no
    credentials. The app ran without SP-API for its whole life and must keep doing so —
    a 500 here would break the Shipment page for a feature the owner may not have set up.
    """
    from app.shipment import spapi

    if not get_settings().spapi_configured:
        return JSONResponse({
            "configured": False,
            "shipments": [],
            "message": "Amazon API is not set up. Add SP_API_CLIENT_ID, "
                       "SP_API_CLIENT_SECRET and SP_API_REFRESH_TOKEN to .env.",
        })

    try:
        # Five, not ten. Each shipment costs two round trips to Amazon's EU endpoint and
        # their rate limit is 2/second, so ten measured 10.4s against 7.6s for five — and
        # the shipment being invoiced is always among the most recent one or two. A
        # lookup slow enough to feel broken gets clicked twice.
        shipments = await spapi.recent_shipments(limit=5)
    except spapi.SpApiError as exc:
        # Amazon's own message is surfaced rather than paraphrased. It is written for a
        # developer, and it is what told us "not supported for the Indian marketplace"
        # instead of leaving us guessing at a permissions problem.
        logger.warning("amazon-shipments lookup failed: %s", exc.message)
        return JSONResponse(
            {"configured": True, "shipments": [], "error": exc.message},
            status_code=502,
        )

    return JSONResponse({
        "configured": True,
        "shipments": [s.as_dict() for s in shipments],
    })


@router.get("/fcs")
async def list_fcs(request: Request, role: str = Depends(require_admin)):
    """The destination FCs the owner can pick, for the shipment-file picker.

    Served rather than hardcoded in the template so the list cannot drift from the one
    `get_fc_info` resolves against — a picker offering a code the invoice cannot resolve
    would put a blank GSTIN on a GST document.

    `has_gstin` travels with each entry because it is the difference between an FC we may
    legally ship to and one we may not: India requires the destination FC to be an
    Additional Place of Business on a GST registration in that state, and we hold no
    registration for 9 of the 93 (Madhya Pradesh, Kerala, Andhra Pradesh).
    """
    return JSONResponse({
        "fcs": [
            {
                "code": c["code"],
                "state": c["state"],
                "has_gstin": bool(c["gstin"]),
                "priority": c["priority"],
            }
            for c in FC_CHOICES
        ]
    })


@router.get("/active")
async def get_active(
    request: Request,
    db: AsyncSession = Depends(get_db),
    role: str = Depends(require_ops_or_admin),
):
    """The plan being packed: items in canonical order, days, thresholds, role.

    `role` is included so the frontend can hide controls it is not allowed to
    use. It is a convenience, never the enforcement — the server re-checks on
    every mutating route.
    """
    plan = await repository.get_active_plan(db)
    if plan is None:
        return JSONResponse({"plan": None, "items": [], "days": [], "role": role})

    items = await repository.load_plan_items(db, plan.id)
    days = await repository.load_days_with_entries(db, plan.id)

    payload = _plan_payload(plan, items, days)
    payload["role"] = role
    payload["missing_sku_count"] = await repository.count_items_missing_sku(db, plan.id)
    return JSONResponse(payload)


@router.post("/items")
async def update_items(
    request: Request,
    db: AsyncSession = Depends(get_db),
    role: str = Depends(require_admin),
):
    """Owner edits to plan quantities and the S/M/B flags.

    Admin-only, and structurally incapable of writing packing data: the
    repository whitelists which columns this may touch. That is what stops the
    old clobbering bug from returning in a new shape.
    """
    body, error = await _json_object(request)
    if error:
        return error
    plan_id = body.get("plan_id")
    updates = body.get("items") or []

    plan = (
        await repository.get_plan(db, int(plan_id))
        if plan_id
        else await repository.get_active_plan(db)
    )
    if plan is None:
        return JSONResponse({"error": "No active plan"}, status_code=404)

    changed = await repository.update_plan_items(db, plan.id, updates)
    return JSONResponse({"status": "saved", "changed": changed})


@router.patch("/plan/{plan_id}/thresholds")
async def patch_thresholds(
    plan_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    role: str = Depends(require_admin),
):
    """Adjust the carry-over thresholds for this plan."""
    body, error = await _json_object(request)
    if error:
        return error

    # Rejected here rather than in the repository, which does `int(value)` and would
    # raise a 500 on "abc" — these two numbers decide whether a day is HELD, so a
    # value that cannot be parsed must not be guessed at.
    values = {}
    for field in ("min_cartons", "min_units"):
        if body.get(field) is None:
            values[field] = None
            continue
        try:
            values[field] = int(body[field])
        except (TypeError, ValueError):
            return JSONResponse(
                {"error": f"{field} must be a whole number."}, status_code=400
            )

    plan = await repository.update_thresholds(
        db, plan_id, values["min_cartons"], values["min_units"]
    )
    if plan is None:
        return JSONResponse({"error": "Plan not found"}, status_code=404)
    return JSONResponse(
        {
            "status": "saved",
            "min_cartons": int(plan.min_cartons or 0),
            "min_units": int(plan.min_units or 0),
        }
    )


@router.delete("/plan/{plan_id}")
async def remove_plan(
    plan_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    role: str = Depends(require_admin),
):
    """Delete a plan and all its packing history. Admin only, and destructive."""
    if not await repository.delete_plan(db, plan_id):
        return JSONResponse({"error": "Plan not found"}, status_code=404)
    return JSONResponse({"status": "cleared"})


# ─── Draft preparation (admin) ───────────────────────────────────────────────
#
# A generated plan is a DRAFT: the owner's to edit, invisible to the warehouse.
# /finalise is the single moment it replaces what the packer is working from.

@router.get("/draft")
async def get_draft(
    request: Request,
    db: AsyncSession = Depends(get_db),
    role: str = Depends(require_admin),
):
    """The plan being prepared, if any. Admin only — a draft is not packable."""
    plan = await repository.get_draft_plan(db)
    if plan is None:
        return JSONResponse({"plan": None, "items": [], "days": [], "role": role})

    # include_excluded so the owner can see and restore what he removed. This is
    # one of only two places that should ever pass it.
    items = await repository.load_plan_items(db, plan.id, include_excluded=True)

    # The days are LOADED, not passed as []. That empty list was correct while a draft
    # could never have packing days — no packing endpoint can reach one — and it became
    # wrong the moment closing a plan started carrying days ONTO a draft.
    #
    # Found in a browser: after a close, this screen showed Kulthi Sattu "500 still to
    # pack" while /plan/{id}/detail said 100 for the same plan, and the carried day was
    # missing from the cards entirely. The packer would have been told to box 400 units
    # that were already in cartons — the exact failure the whole carry design exists to
    # prevent, reintroduced by one hardcoded argument.
    days = await repository.load_days_with_entries(db, plan.id)
    payload = _plan_payload(plan, items, days)
    payload["role"] = role
    payload["missing_sku_count"] = await repository.count_items_missing_sku(db, plan.id)
    return JSONResponse(payload)


@router.post("/plan/{plan_id}/finalise")
async def finalise_plan(
    plan_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    role: str = Depends(require_admin),
):
    """Promote a draft to active. This is what "finalise the plan" means.

    Admin only: deciding that the warehouse's plan has changed is the owner's
    call, not the packer's. Closing the previous active plan happens here, in the
    same transaction, rather than at generate — see repository.create_plan.
    """
    plan = await repository.finalise_plan(db, plan_id)
    if plan is None:
        return JSONResponse({"error": "Plan not found"}, status_code=404)

    items = await repository.load_plan_items(db, plan.id)
    days = await repository.load_days_with_entries(db, plan.id)
    payload = _plan_payload(plan, items, days)
    payload["role"] = role
    payload["status"] = "finalised"
    return JSONResponse(payload)


@router.post("/plan/{plan_id}/close")
async def close_plan_route(
    plan_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    role: str = Depends(require_admin),
):
    """Retire a plan, carrying its packed-but-unshipped days onto the next one.

    Distinct from ``/finalise``, which promotes a draft. This is the owner deciding a
    plan is done, which happens BEFORE a replacement exists in the case that prompted
    it: sales data moved, a new plan is wanted, and the last packed day is below the
    carton threshold so it cannot ship on its own.

    The target is the current draft, or a new empty carrier plan when there is none.
    Without the carrier, closing early would refuse and the boxes would have nowhere to
    go — which is how held stock becomes lost stock.

    Refuses with 409, having moved nothing, when a day is still open or has a
    part-created Amazon inbound plan. Shipped-but-uninvoiced days do not block: they
    stay on this plan and are named in the response, because the owner may well have
    invoiced them outside the app.
    """
    plan = await repository.get_plan(db, plan_id)
    if plan is None:
        return JSONResponse({"error": "No such plan."}, status_code=404)
    if plan.status == repository.STATUS_CLOSED:
        return JSONResponse(
            {"error": f"{plan.label or 'That plan'} is already closed."},
            status_code=409,
        )

    days = await repository.load_days_with_entries(db, plan_id)
    split = logic.carriable_days(days)

    # Refuse BEFORE creating anything. `close_plan` refuses on the same condition, but
    # by then a carrier plan would already exist — and an abandoned draft is not inert:
    # `get_draft_plan` would hand it to the owner as the plan he is editing, and the next
    # /generate silently deletes whatever draft it finds. So a refused close would leave
    # a phantom plan on screen and then destroy it without a word.
    #
    # Checked here rather than only inside close_plan because this is the only layer that
    # knows a plan is about to be created.
    if split["blocked"]:
        named = "; ".join(f"{b['pack_date']}: {b['reason']}" for b in split["blocked"])
        return JSONResponse(
            {
                "error": f"The plan was not closed and nothing was moved. {named}",
                "blocked": split["blocked"],
            },
            status_code=409,
        )

    # The target is resolved only when something actually needs carrying, so closing a
    # fully-shipped plan does not leave an empty plan behind.
    target_id = None
    created_carrier = False
    if split["carry"]:
        draft = await repository.get_draft_plan(db)
        if draft is not None:
            target_id = draft.id
        else:
            carrier = await repository.create_plan(
                db, [],
                label=f"Carried from {plan.label or f'plan {plan_id}'}",
                status=repository.STATUS_DRAFT,
            )
            target_id = carrier.id
            created_carrier = True

    result = await repository.close_plan(db, plan_id, target_id)

    # Still checked: close_plan re-reads the days and can refuse for a reason that
    # appeared between the two reads, and it owns the no-target refusal.
    if not result["closed"]:
        named = "; ".join(
            f"{b['pack_date']}: {b['reason']}" for b in result["blocked"]
        )
        return JSONResponse(
            {
                "error": f"The plan was not closed and nothing was moved. {named}",
                "blocked": result["blocked"],
            },
            status_code=409,
        )

    result["created_carrier_plan"] = created_carrier
    warnings = []
    if result["shipped_uninvoiced"]:
        warnings.append(
            f"{len(result['shipped_uninvoiced'])} shipped day(s) have no invoice "
            f"({', '.join(result['shipped_uninvoiced'])}). They stay on this plan and "
            "can still be invoiced from Plan history."
        )
    if result["orphan_asins"]:
        warnings.append(
            f"{len(result['orphan_asins'])} carried SKU(s) are not in the new plan and "
            "were added with To Ship 0, so the packed boxes still reach an invoice: "
            f"{', '.join(result['orphan_asins'])}."
        )
    if warnings:
        result["warning"] = " ".join(warnings)
    return JSONResponse(result)


@router.get("/plans")
async def list_plans_route(
    request: Request,
    db: AsyncSession = Depends(get_db),
    role: str = Depends(require_admin),
):
    """Every plan, newest first, for the history screen.

    Admin only: the summary carries planned quantities, which are projections, and the
    Accounts preset withholds those for the same reason it withholds purchase costs.
    """
    return JSONResponse({"plans": await repository.list_plans(db)})


@router.get("/plan/{plan_id}/detail")
async def plan_detail(
    plan_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    role: str = Depends(require_admin),
):
    """One plan in full, in the SAME shape /active returns.

    Deliberately identical, so the history screen renders through the existing code
    rather than a second renderer that could disagree with it about order or any
    computed number — the same reason all four documents share ``_document_rows``.
    """
    plan = await repository.get_plan(db, plan_id)
    if plan is None:
        return JSONResponse({"error": "No such plan."}, status_code=404)

    items = await repository.load_plan_items(db, plan_id)
    days = await repository.load_days_with_entries(db, plan_id)
    payload = _plan_payload(plan, items, days)
    payload["role"] = role
    payload["read_only"] = plan.status == repository.STATUS_CLOSED
    payload["plan"]["closed_at"] = (
        plan.closed_at.isoformat() if plan.closed_at else None
    )
    return JSONResponse(payload)


@router.post("/plan/{plan_id}/items/exclude")
async def exclude_items(
    plan_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    role: str = Depends(require_admin),
):
    """Remove rows from a plan, reversibly. Body: {"asins": [...], "excluded": bool}

    **Refuses to exclude a row that already has boxes packed against it**, and
    that refusal is the whole reason this endpoint is not a one-liner.
    ``logic.packed_units_by_asin`` aggregates packing entries by ASIN and never
    consults plan items, while the invoice bridge builds its lines FROM plan
    items. So an excluded-but-packed row means real boxes ship with no GST line
    against them — an under-statement on a tax document, discovered at
    reconciliation rather than on any screen here.

    The owner is pointed at the correct move instead: set To Ship to 0, which
    stops further packing while keeping the row, its history and its invoice line.
    """
    body, error = await _json_object(request)
    if error:
        return error
    asins = [str(a).strip() for a in (body.get("asins") or []) if str(a).strip()]
    excluded = bool(body.get("excluded", True))

    if not asins:
        return JSONResponse({"error": "Select at least one row."}, status_code=400)

    plan = await repository.get_plan(db, plan_id)
    if plan is None:
        return JSONResponse({"error": "Plan not found"}, status_code=404)

    if excluded:
        days = await repository.load_days_with_entries(db, plan.id)
        packed = logic.packed_units_by_asin(days)
        blocked = []
        for asin in asins:
            units = int(packed.get(asin, 0))
            if units <= 0:
                continue
            dates = sorted(
                d["pack_date"]
                for d in days
                for e in (d.get("entries") or [])
                if e.get("asin") == asin and int(e.get("units") or 0) > 0
            )
            blocked.append({"asin": asin, "units": units, "dates": dates})

        if blocked:
            detail = "; ".join(
                f"{b['asin']} ({b['units']} units on {', '.join(b['dates'])})"
                for b in blocked
            )
            return JSONResponse(
                {
                    "error": (
                        f"Cannot remove {detail}. Those boxes are already packed, and "
                        "removing the row would drop them from the invoice — the "
                        "stock would ship with nothing billed against it. Set To Ship "
                        "to 0 instead: packing stops and the record is kept."
                    ),
                    "blocked": blocked,
                },
                status_code=409,
            )

    changed = await repository.set_item_excluded(db, plan.id, asins, excluded)
    return JSONResponse(
        {
            "status": "excluded" if excluded else "restored",
            "changed": changed,
            "count": len(changed),
        }
    )


# ─── Product sort priority (admin) ───────────────────────────────────────────

@router.get("/categories")
async def get_categories(
    request: Request,
    db: AsyncSession = Depends(get_db),
    role: str = Depends(require_admin),
):
    """Every product's sort priority, for the editor.

    Keyword defaults are seeded at generate time; `source` says whether a value
    was guessed or chosen, so the screen can show which still need a look.
    """
    rows = await repository.load_categories(db)
    return JSONResponse(
        {
            "labels": logic.CATEGORY_LABELS,
            "categories": [
                {
                    "product_key": r.product_key,
                    "product_label": r.product_label or r.product_key,
                    "priority": int(r.priority or logic.DEFAULT_CATEGORY),
                    "source": r.source or "keyword",
                }
                for r in rows
            ],
        }
    )


@router.patch("/categories")
async def patch_categories(
    request: Request,
    db: AsyncSession = Depends(get_db),
    role: str = Depends(require_admin),
):
    """Override sort priorities. Body: {"categories": {"chana sattu": 2, ...}}

    Applies immediately to every plan, current and future, because
    load_plan_items reads the priority at query time rather than from a copy
    stored on the row.
    """
    body, error = await _json_object(request)
    if error:
        return error

    # A mapping, or nothing. `set_categories` calls `.items()`, which a list or a
    # string does not have — that was a 500 on valid-but-wrong-shaped JSON.
    categories = body.get("categories") or {}
    if not isinstance(categories, dict):
        return JSONResponse(
            {"error": 'categories must be an object like {"chana sattu": 2}.'},
            status_code=400,
        )

    changed = await repository.set_categories(db, categories)
    return JSONResponse({"status": "saved", "changed": changed})


# ─── Daily packing (ops + admin) ─────────────────────────────────────────────

def _valid_date(value: str) -> bool:
    try:
        date.fromisoformat(value)
        return True
    except (TypeError, ValueError):
        return False


async def _json_object(request: Request) -> tuple[dict | None, JSONResponse | None]:
    """The request body as a dict, or (None, 400 response).

    Every mutating route here reads ``body.get(...)``, which raises AttributeError on
    a body that is not a JSON object — and an unhandled AttributeError is a 500. QA
    found that `null`, `[]` and `"str"` each 500'd on **six** endpoints: valid JSON,
    wrong shape, no validation between them.

    A 500 is not merely untidy here. It is indistinguishable from the server being
    broken, so the packer's real reaction is to retry, and on a flaky warehouse
    connection a retry against a route that half-committed is how duplicate packing
    rows appear. A 400 says "your request was wrong" and ends it.

    Returns a tuple rather than raising so each caller stays a plain `return`, which
    is how every other error path in this file already reads.
    """
    try:
        body = await request.json()
    except Exception:
        return None, JSONResponse({"error": "Body must be JSON."}, status_code=400)
    if not isinstance(body, dict):
        return None, JSONResponse(
            {"error": "Body must be a JSON object, for example {\"items\": [...]}."},
            status_code=400,
        )
    return body, None


@router.get("/packing/{pack_date}")
async def get_packing(
    pack_date: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    role: str = Depends(require_ops_or_admin),
):
    """What was entered for one date, plus what is still outstanding per SKU.

    Returns every planned SKU, not only the ones already touched — this is the
    list the packer works down, so a SKU with nothing entered yet must appear.
    """
    if not _valid_date(pack_date):
        return JSONResponse({"error": "Date must be YYYY-MM-DD"}, status_code=400)

    plan = await repository.get_active_plan(db)
    if plan is None:
        return JSONResponse({"error": "No active plan"}, status_code=404)

    items = await repository.load_plan_items(db, plan.id)
    days = await repository.load_days_with_entries(db, plan.id)

    today = next((d for d in days if d["pack_date"] == pack_date), None)
    entered = {e["asin"]: e for e in (today or {}).get("entries", [])}

    # Packed on OTHER days, so "remaining" on this screen does not subtract what
    # the packer is entering right now and make the target appear to move.
    other_days = [d for d in days if d["pack_date"] != pack_date]
    packed_elsewhere = logic.packed_units_by_asin(other_days)

    rows = []
    for item in items:
        planned = int(item.shipment_plan or 0)
        if planned <= 0:
            continue
        prior = packed_elsewhere.get(item.asin, 0)
        mine = entered.get(item.asin) or {}
        rows.append(
            {
                "asin": item.asin,
                "fba_sku": item.fba_sku or "",
                "item": item.item or "",
                "weight": float(item.weight or 0),
                # Same label the owner's screen and the printed sheet show. The
                # packer reads the morning PDF and this screen side by side, so
                # "500 g" on paper and "0.5kg" on the phone is a needless
                # translation for him to do on every row.
                "weight_label": logic.weight_label(item.weight),
                "planned": planned,
                "packed_before": prior,
                "remaining": logic.remaining_for(planned, prior),
                "units": int(mine.get("units") or 0),
                # Beyond the plan across ALL days including this one, so the packer
                # is warned by the total that exists rather than only by what he
                # typed just now. `remaining` above excludes today deliberately (so
                # the target does not appear to move as he types); this must not,
                # because 40 over on Monday plus 40 today is 80 over.
                "over_packed": logic.over_packed(
                    planned, prior + int(mine.get("units") or 0)
                ),
                "note": mine.get("note") or "",
            }
        )

    return JSONResponse(
        {
            "plan_id": plan.id,
            "pack_date": pack_date,
            "role": role,
            "day": today,
            "status": (today or {}).get("status", logic.STATUS_OPEN),
            "min_cartons": int(plan.min_cartons or 0),
            "min_units": int(plan.min_units or 0),
            # One number for the whole day, not a column on every row.
            "cartons": int((today or {}).get("total_cartons") or 0),
            "items": rows,
        }
    )


@router.post("/packing/{pack_date}")
async def save_packing(
    pack_date: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    role: str = Depends(require_ops_or_admin),
):
    """Upsert per-SKU units and the day's carton count. The only write ops performs.

    Body: ``{"entries": [{"asin": ..., "units": ...}], "cartons": 20}``.

    ``cartons`` is the whole day's box count, not a per-SKU figure — "500 units
    packed today in 20 cartons". Omitting the key leaves the stored count untouched,
    which is what lets a partial save of unit counts not wipe it; sending 0 clears it.

    Refuses once the day is verified or shipped: an invoice may already carry
    those numbers, and silently editing them would put the GST document and the
    warehouse record out of agreement.

    Entries for rows the owner has since EXCLUDED are dropped rather than stored,
    and reported back. This closes the stale-screen race: the packer's phone still
    lists a row the owner removed a minute ago, and accepting that count would
    manufacture exactly the state /items/exclude refuses to create — packed units
    against a row that appears on no document and no invoice.

    The whole save is not rejected over it. His other counts are real, manual work
    and must not be thrown away because one row went stale; the dropped ASINs come
    back so the screen can say what happened and refresh.
    """
    if not _valid_date(pack_date):
        return JSONResponse({"error": "Date must be YYYY-MM-DD"}, status_code=400)

    body, error = await _json_object(request)
    if error:
        return error

    # A 400 rather than tolerating it, because a non-list `entries` means the caller
    # is confused about the shape, and silently treating it as "no entries" would
    # answer 200 "saved" to a request that saved nothing. Found by QA: posting
    # `"entries": "oops"` used to reach `raw.get()` below and 500.
    entries = body.get("entries") or []
    if not isinstance(entries, list):
        return JSONResponse(
            {"error": "entries must be a list of {asin, units} objects."},
            status_code=400,
        )

    # `None` means "not sent", which must not be confused with 0 ("no cartons"). A
    # save posted before the packer reaches the carton box would otherwise clear a
    # count he entered earlier — and that number ends up on a GST document.
    cartons = body.get("cartons")
    if cartons is not None:
        try:
            cartons = max(0, int(cartons))
        except (TypeError, ValueError):
            return JSONResponse(
                {"error": "cartons must be a whole number."}, status_code=400
            )

    plan = await repository.get_active_plan(db)
    if plan is None:
        return JSONResponse({"error": "No active plan"}, status_code=404)

    existing = await repository.get_day(db, plan.id, pack_date)
    if existing is not None and existing.status in (
        logic.STATUS_VERIFIED,
        logic.STATUS_SHIPPED,
    ):
        return JSONResponse(
            {
                "error": f"{pack_date} is already {existing.status} and cannot be "
                "edited. Ask the owner to reopen it."
            },
            status_code=409,
        )

    included = {
        i.asin for i in await repository.load_plan_items(db, plan.id)
    }
    kept, dropped = [], []
    for raw in entries:
        # isinstance, not `(raw or {})` — that guards None and nothing else, so a
        # bare string in the list reached .get() and 500'd. The repository skips
        # non-dicts too, but this loop runs first, so the check has to be here.
        if not isinstance(raw, dict):
            continue
        asin = str(raw.get("asin") or "").strip()
        if asin and asin not in included:
            dropped.append(asin)
            continue
        kept.append(raw)

    day = await repository.save_packing_entries(
        db, plan.id, pack_date, kept, submitted_by=role, cartons=cartons
    )
    payload = {
        "status": "saved",
        "pack_date": day.pack_date,
        "day_status": day.status,
        "total_units": int(day.total_units or 0),
        "total_cartons": int(day.total_cartons or 0),
    }
    if dropped:
        payload["dropped"] = dropped
        payload["warning"] = (
            f"{len(dropped)} item(s) were removed from the plan by the owner and "
            "were not saved. Refresh to see the current list."
        )
    return JSONResponse(payload)


@router.post("/packing/{pack_date}/submit")
async def submit_packing(
    pack_date: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    role: str = Depends(require_ops_or_admin),
):
    """Close a day's packing → `submitted`, or `held` if it is too small.

    The hold is a suggestion the system makes, not a lock: the owner can always
    force it through with /release. Holding is what requirement 9 asked for —
    a 20-carton/400-unit day waits and combines with tomorrow rather than
    becoming its own uneconomic shipment.
    """
    if not _valid_date(pack_date):
        return JSONResponse({"error": "Date must be YYYY-MM-DD"}, status_code=400)

    plan = await repository.get_active_plan(db)
    if plan is None:
        return JSONResponse({"error": "No active plan"}, status_code=404)

    day = await repository.get_day(db, plan.id, pack_date)
    if day is None or (day.total_units == 0 and day.total_cartons == 0):
        return JSONResponse(
            {"error": f"Nothing recorded for {pack_date} yet."}, status_code=400
        )

    if day.status in (logic.STATUS_VERIFIED, logic.STATUS_SHIPPED):
        return JSONResponse(
            {"error": f"{pack_date} is already {day.status}."}, status_code=409
        )

    held = logic.is_held(
        day.total_cartons, day.total_units, plan.min_cartons, plan.min_units
    )
    day.status = logic.STATUS_HELD if held else logic.STATUS_SUBMITTED
    day.hold_reason = (
        logic.hold_reason(
            day.total_cartons, day.total_units, plan.min_cartons, plan.min_units
        )
        if held
        else None
    )
    day.submitted_by = role
    day.submitted_at = datetime.utcnow()
    await db.commit()
    await db.refresh(day)

    return JSONResponse(
        {
            "status": day.status,
            "held": held,
            "hold_reason": day.hold_reason,
            "total_units": int(day.total_units or 0),
            "total_cartons": int(day.total_cartons or 0),
        }
    )


@router.post("/packing/{pack_date}/verify")
async def verify_packing(
    pack_date: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    role: str = Depends(require_admin),
):
    """Owner approval. Admin only — this is what gates the GST invoice."""
    if not _valid_date(pack_date):
        return JSONResponse({"error": "Date must be YYYY-MM-DD"}, status_code=400)

    plan = await repository.get_active_plan(db)
    if plan is None:
        return JSONResponse({"error": "No active plan"}, status_code=404)

    day = await repository.get_day(db, plan.id, pack_date)
    if day is None:
        return JSONResponse({"error": f"No packing for {pack_date}"}, status_code=404)

    if day.status == logic.STATUS_OPEN:
        return JSONResponse(
            {"error": f"{pack_date} has not been submitted yet."}, status_code=400
        )
    if day.status == logic.STATUS_SHIPPED:
        return JSONResponse(
            {"error": f"{pack_date} is already shipped."}, status_code=409
        )

    # A held day can be verified directly: verifying IS the owner deciding the
    # units are good, and requiring a separate release first would be two clicks
    # for one decision.
    day.status = logic.STATUS_VERIFIED
    day.hold_reason = None
    day.verified_at = datetime.utcnow()
    await db.commit()
    await db.refresh(day)
    return JSONResponse({"status": day.status, "verified_at": day.verified_at.isoformat()})


@router.post("/packing/{pack_date}/reopen")
async def reopen_packing(
    pack_date: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    role: str = Depends(require_ops_or_admin),
):
    """Send a verified day back to `open` so the warehouse can correct it.

    Asked for directly: "even after the day is verified or submitted. give option for
    the warehouse team to edit the units and carton and then resubmit."

    **Ops, not admin.** A miscount is discovered on the floor, by the person who did
    the counting, and the correction is the same manual work as the original entry. The
    old advice — the 409 from `save_packing` and the banner on the packing screen both
    said "ask the owner to reopen it" — pointed at a route that did not exist anywhere
    in the app, so the only real recovery was to ask the owner to run SQL.

    **Refused once an invoice is attached (409).** That is the one case the warehouse
    cannot be allowed to fix quietly: `/invoice/save` has already spent a number from a
    legally-sequential GST series against these exact quantities, so changing them here
    would leave the tax document and the warehouse record disagreeing with nothing in
    the app able to detect it afterwards. The error names the invoice so the owner knows
    what he is being asked about.

    Verification is cleared, not kept. The owner's approval is what gates a GST invoice,
    so it has to refer to the numbers actually on the day — leaving `verified` in place
    would make his sign-off cover figures he never saw. Back to `open` rather than
    `submitted` because the day is being edited again, and `submit` re-applies the
    hold threshold to whatever the corrected totals turn out to be.
    """
    if not _valid_date(pack_date):
        return JSONResponse({"error": "Date must be YYYY-MM-DD"}, status_code=400)

    plan = await repository.get_active_plan(db)
    if plan is None:
        return JSONResponse({"error": "No active plan"}, status_code=404)

    day = await repository.get_day(db, plan.id, pack_date)
    if day is None:
        return JSONResponse({"error": f"No packing for {pack_date}"}, status_code=404)

    # Checked before the status, and deliberately: a shipped day is invoiced by
    # definition, and "invoice ST/26-27/031 already covers these boxes" is the reason,
    # where "it is shipped" only restates the symptom.
    if day.invoice_id:
        # `invoice_no` ("ST/26-27/028"), NOT `invoice_number` — the latter is the bare
        # sequence integer (28), which on screen would read as "invoice 28" and match
        # nothing the owner can search for in his own records.
        numbers = await _invoice_numbers(db, [day.invoice_id])
        number = numbers.get(day.invoice_id) or f"#{day.invoice_id}"
        return JSONResponse(
            {
                "error": f"{pack_date} is already on invoice {number}, so its units "
                "and cartons cannot be changed here. That invoice has a GST number "
                "against these exact quantities — editing them would leave the tax "
                "document and the packing record disagreeing. Ask the owner: the "
                "invoice has to be dealt with first.",
                "invoice_id": day.invoice_id,
                "invoice_number": number,
            },
            status_code=409,
        )

    if day.status == logic.STATUS_OPEN:
        return JSONResponse(
            {"error": f"{pack_date} is already open for editing."}, status_code=409
        )

    day.status = logic.STATUS_OPEN
    day.hold_reason = None
    day.submitted_at = None
    day.verified_at = None
    await db.commit()
    await db.refresh(day)
    return JSONResponse({"status": day.status, "pack_date": pack_date})


@router.post("/packing/{pack_date}/release")
async def release_packing(
    pack_date: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    role: str = Depends(require_admin),
):
    """Force a held day back to `submitted` — ship it anyway.

    The threshold suggests; the owner decides. Without this the system could park
    stock indefinitely on its own judgement.
    """
    if not _valid_date(pack_date):
        return JSONResponse({"error": "Date must be YYYY-MM-DD"}, status_code=400)

    plan = await repository.get_active_plan(db)
    if plan is None:
        return JSONResponse({"error": "No active plan"}, status_code=404)

    day = await repository.get_day(db, plan.id, pack_date)
    if day is None:
        return JSONResponse({"error": f"No packing for {pack_date}"}, status_code=404)
    if day.status != logic.STATUS_HELD:
        return JSONResponse(
            {"error": f"{pack_date} is {day.status}, not held."}, status_code=409
        )

    day.status = logic.STATUS_SUBMITTED
    day.hold_reason = None
    await db.commit()
    await db.refresh(day)
    return JSONResponse({"status": day.status})


# ─── Downloads ───────────────────────────────────────────────────────────────
#
# Every route here goes through _document_rows(), which returns the SAME dicts
# /active sends to the browser, in the SAME order. So a download cannot disagree
# with the screen about either row order or a computed number — there is one
# code path producing both, not two that happen to agree today.

async def _document_rows(db: AsyncSession, plan_id: int | None = None):
    """(plan, item dicts in canonical order, days) or None if there is no such plan.

    ``plan_id`` defaults to the active plan, which is every existing caller's
    behaviour. Passing one is what lets accounts reprint a CLOSED plan's packed sheet —
    before this, every download resolved only the active plan, so the moment a plan
    closed its documents became unavailable with the data still sitting in the table.

    One function, so all five downloads inherit plan targeting together and none can
    drift — the same single-source property load_plan_items has for row order.
    """
    plan = (
        await repository.get_plan(db, plan_id)
        if plan_id
        else await repository.get_active_plan(db)
    )
    if plan is None:
        return None

    items = await repository.load_plan_items(db, plan.id)
    days = await repository.load_days_with_entries(db, plan.id)
    packed_map = logic.packed_units_by_asin(days)
    shippable_map = logic.shippable_units_by_asin(days)
    rows = [
        _item_payload(i, packed_map.get(i.asin, 0), shippable_map.get(i.asin, 0))
        for i in items
    ]
    plan_dict = {
        "id": plan.id,
        "label": plan.label,
        "min_cartons": int(plan.min_cartons or 0),
        "min_units": int(plan.min_units or 0),
    }
    return plan_dict, rows, days


def _no_plan():
    return JSONResponse({"error": "No active plan to download."}, status_code=404)


def _attachment(buffer: io.BytesIO, filename: str, content_type: str) -> StreamingResponse:
    """Send an in-memory document as a download.

    A dated filename on purpose: the owner keeps these in a folder and
    `packed.xlsx` overwriting last week's `packed.xlsx` in the Downloads folder
    is a real way to lose a record.
    """
    return StreamingResponse(
        buffer,
        media_type=content_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


XLSX_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


# The three documents the owner asked for, each in both formats, all sharing one
# column layout: S · M · B · Brand · ASIN · SKU · Product · quantity.
#
# `fmt` is a path parameter rather than two routes per document, so the Excel and
# the PDF of one document are guaranteed to be built from the same rows by the same
# code. Two routes would be two places for a filter to drift.


def _document(fmt: str, title: str, subtitle: str, headers, rows, widths, stem: str):
    """Render rows as xlsx or pdf and return it as a download."""
    if fmt == "xlsx":
        return _attachment(
            documents.build_simple_xlsx(title, subtitle, headers, rows, widths),
            f"{stem}.xlsx",
            XLSX_TYPE,
        )
    return _attachment(
        documents.build_simple_pdf(title, subtitle, headers, rows),
        f"{stem}.pdf",
        "application/pdf",
    )


def _bad_format(fmt: str):
    return JSONResponse(
        {"error": f"Unknown format '{fmt}'. Use xlsx or pdf."}, status_code=404
    )


@router.get("/download/plan.{fmt}")
async def download_plan(
    fmt: str,
    request: Request,
    plan_id: int | None = None,
    db: AsyncSession = Depends(get_db),
    role: str = Depends(require_admin),
):
    """The shipment plan: ONLY the rows with something to ship.

    "not the entire list of skus" — a 205-row sheet where 88 rows read 0 is a
    sheet nobody reads to the end. build_simple rows drop anything with a zero
    quantity, so this is the working document.
    """
    if fmt not in ("xlsx", "pdf"):
        return _bad_format(fmt)

    loaded = await _document_rows(db, plan_id)
    if loaded is None:
        return _no_plan()
    plan, rows, _days = loaded

    lines = documents._rows_with_quantity(rows, "shipment_plan")
    today = date.today().isoformat()
    return _document(
        fmt,
        "Shipment Plan",
        f"{plan.get('label') or 'Plan'} · generated {today}",
        documents.IDENTITY_HEADERS + ["To Pack"],
        lines,
        documents.IDENTITY_WIDTHS + [12],
        f"shipment-plan-{today}",
    )


@router.get("/download/packed.{fmt}")
async def download_packed(
    fmt: str,
    request: Request,
    date_from: str | None = None,
    date_to: str | None = None,
    plan_id: int | None = None,
    db: AsyncSession = Depends(get_db),
    role: str = Depends(require_ops_or_admin),
):
    """What was actually packed, over a date or a range.

    Both dates blank means every packing day on the plan, which is the safe
    default: a blank field should not silently narrow a report.

    **Units per SKU; cartons per day, in the heading.** There is no per-row carton
    column, and there cannot be a meaningful one: a carton holds whatever was being
    packed when it was filled, so the boxes belong to the day rather than to any
    single ASIN. The sheet used to carry a per-SKU Cartons column and it was
    reporting a number the packer had guessed. The carton count still travels,
    because it is what prefills a GST invoice's Boxes field — it travels as the fact
    it actually is.

    This is the document ops prints for the accounts team, so it is open to ops. It
    carries only what was packed: no projections, no purchase-driven figures.
    """
    if fmt not in ("xlsx", "pdf"):
        return _bad_format(fmt)
    for value in (date_from, date_to):
        if value and not _valid_date(value):
            return JSONResponse({"error": "Dates must be YYYY-MM-DD"}, status_code=400)

    loaded = await _document_rows(db, plan_id)
    if loaded is None:
        return _no_plan()
    plan, rows, days = loaded

    chosen = [
        d for d in days
        if (not date_from or d["pack_date"] >= date_from)
        and (not date_to or d["pack_date"] <= date_to)
    ]
    units = logic.units_by_asin(chosen)

    lines = []
    for item in rows:
        packed_units = int(units.get(item["asin"], 0))
        if packed_units <= 0:
            continue
        lines.append(documents._identity_cells(item) + [packed_units])

    span = (
        f"{date_from or 'start'} to {date_to or 'today'}"
        if (date_from or date_to)
        else f"all {len(chosen)} packing day(s)"
    )
    # Named per day rather than only totalled, because the accounts team reconciles
    # a shipment against the days that went into it, and "38 cartons" alone cannot be
    # checked against anything.
    per_day = " · ".join(
        f"{d['pack_date']}: {logic.day_cartons(d)}"
        for d in chosen
        if logic.day_cartons(d)
    )
    total_cartons = sum(logic.day_cartons(d) for d in chosen)
    cartons_note = (
        f" · {total_cartons} cartons ({per_day})" if per_day else ""
    )
    stamp = date_from or date.today().isoformat()
    return _document(
        fmt,
        "Packed",
        f"{plan.get('label') or 'Plan'} · {span}{cartons_note}",
        documents.IDENTITY_HEADERS + ["Units"],
        lines,
        documents.IDENTITY_WIDTHS + [12],
        f"packed-{stamp}",
    )


@router.get("/download/remaining.{fmt}")
async def download_remaining(
    fmt: str,
    request: Request,
    pack_date: str | None = None,
    plan_id: int | None = None,
    db: AsyncSession = Depends(get_db),
    role: str = Depends(require_ops_or_admin),
):
    """What is still to pack against the finalised plan — the morning sheet.

    Open to ops, unlike the other two: this is the one document the packer needs
    every morning, and making him ask for it would defeat the point of his own
    screen. It carries no projections or purchase-driven numbers, so there is
    nothing on it he should not see.

    ``pack_date`` labels the sheet and its filename and nothing else. The packer
    pulls it against the date he is working on, and a page headed "today" while he
    is entering yesterday's counts gets filed wrongly. It deliberately does NOT
    filter the rows: what is left to pack is a running total against the plan, not
    a per-day figure.
    """
    if fmt not in ("xlsx", "pdf"):
        return _bad_format(fmt)
    if pack_date is not None and not _valid_date(pack_date):
        return JSONResponse({"error": "Date must be YYYY-MM-DD"}, status_code=400)

    loaded = await _document_rows(db, plan_id)
    if loaded is None:
        return _no_plan()
    plan, rows, _days = loaded

    lines = documents._rows_with_quantity(rows, "remaining")
    stamp = pack_date or date.today().isoformat()
    return _document(
        fmt,
        "Still To Pack",
        f"{plan.get('label') or 'Plan'} · as at {stamp}",
        documents.IDENTITY_HEADERS + ["To Pack"],
        lines,
        documents.IDENTITY_WIDTHS + [12],
        f"still-to-pack-{stamp}",
    )


@router.get("/download/shipment-file.xlsx")
async def download_shipment_file_xlsx(
    request: Request,
    mode: str = "remaining",
    pack_dates: str = "",
    fc_code: str = "",
    plan_id: int | None = None,
    db: AsyncSession = Depends(get_db),
    role: str = Depends(require_admin),
):
    """The Amazon upload sheet: merchant SKU + quantity.

    An unknown `mode` is rejected rather than quietly treated as `remaining`. The
    three modes produce genuinely different quantities, and a typo silently
    yielding a plausible-looking file is how the wrong numbers get uploaded to
    Amazon.

    ``pack_dates`` is a comma-separated list that narrows `verified` to CHOSEN days —
    the same subset the owner ticks for an invoice. Asked for so the shipment can be
    created in Seller Central from exactly the boxes that are going in this truck:
    ``mode=verified`` alone sweeps up every verified day, which is the same "two days
    that should have been two shipments can only ever be one" problem the invoice
    selection was built to fix.

    It is a query parameter rather than a POST body because this is a download the
    browser navigates to, and the dates are short. Ignored for the other two modes,
    which are about the plan rather than about days.
    """
    if mode not in ("remaining", "all", "verified"):
        return JSONResponse(
            {"error": "mode must be remaining, all or verified"}, status_code=400
        )

    chosen_dates = [d.strip() for d in pack_dates.split(",") if d.strip()]
    invalid = [d for d in chosen_dates if not _valid_date(d)]
    if invalid:
        return JSONResponse(
            {"error": f"Not a date (YYYY-MM-DD): {', '.join(invalid)}"}, status_code=400
        )

    # An unrecognised FC is refused rather than written onto every row. This file
    # decides where real boxes are sent, and a typo like "ISK33" would otherwise
    # produce a plausible-looking sheet naming a warehouse that does not exist — then
    # the same wrong code reaches the invoice, where it resolves to no state and no
    # GSTIN. Cheap to check: we hold all 93 FC codes.
    fc_code = fc_code.strip().upper()
    if fc_code and fc_code not in KNOWN_FC_CODES:
        return JSONResponse(
            {
                "error": f"{fc_code} is not an FC code we know. Check the spelling — "
                "the destination decides the GSTIN on the invoice.",
                "known_example": "ISK3, BLR4, DED3",
            },
            status_code=400,
        )

    loaded = await _document_rows(db, plan_id)
    if loaded is None:
        return _no_plan()
    _plan, rows, days = loaded

    if chosen_dates:
        wanted = set(chosen_dates)
        # Filtered BEFORE the quantities are computed, so `verified` counts only the
        # selected days. Filtering the finished rows instead would leave the totals
        # covering every verified day while the file claimed to be about these ones.
        days = [d for d in days if d["pack_date"] in wanted]

    # Named after the days it covers, not the day it was downloaded: the owner may
    # download the same selection twice and will certainly download two different
    # selections on one afternoon. Joining the dates with '-' made
    # "shipment-verified-2026-08-10-2026-08-11-2026-08-11.xlsx", where the trailing
    # today's-date is indistinguishable from another pack date — and a single-day file
    # collided with the unfiltered one. `_to_` separates a range readably, and today's
    # date is dropped when the dates already identify the file.
    if chosen_dates:
        span = sorted(chosen_dates)
        name = span[0] if len(span) == 1 else f"{span[0]}_to_{span[-1]}"
        filename = f"shipment-{mode}-{name}.xlsx"
    else:
        filename = f"shipment-{mode}-{date.today().isoformat()}.xlsx"
    # The FC in the filename too: two shipments to two warehouses on the same days is
    # a normal week, and they must not overwrite each other in the Downloads folder.
    if fc_code:
        filename = filename.replace(".xlsx", f"-{fc_code}.xlsx")

    return _attachment(
        documents.build_shipment_file_xlsx(
            rows, mode=mode, days=days, fc_code=fc_code
        ),
        filename,
        XLSX_TYPE,
    )


# ─── Invoice bridge (ops + admin) ────────────────────────────────────────────
#
# Requirement 8: "when the data daily is entered and is verified by me, the
# operations team can directly generate invoice using this if they want to."
#
# Two words in that sentence carry all the risk. **"verified by me"** is the
# gate: this endpoint refuses anything less, because the invoice number is a
# legally-sequential GST document and issuing one against numbers the owner has
# not approved cannot be undone by deleting a row. **"if they want to"** is why
# ops may call it at all — but note what it does NOT do: it builds a *payload*.
# `POST /invoice/save` is untouched, still the only thing that allocates an
# invoice number, and its own 26 tests keep guarding that sequence.

def _title_for(item) -> str:
    """A human product title from the plan snapshot.

    The plan stores the parent product and a weight, not Amazon's full listing
    title — the CSVs it is built from do not carry one. So the title is composed,
    and the SKU is appended because two SKUs of the same product and weight would
    otherwise produce two identical invoice lines that nobody could tell apart
    when checking the document against the boxes.
    """
    product = (item.item or "").strip() or item.asin
    weight = float(item.weight or 0)
    parts = [product.title() if product.islower() else product]
    if weight:
        # 0.5 -> "500 g", 1.0 -> "1 kg". Trailing ".0" on a label looks like a bug.
        parts.append(f"{int(weight * 1000)} g" if weight < 1 else f"{weight:g} kg")
    if item.fba_sku:
        parts.append(f"({item.fba_sku})")
    return " ".join(parts)


@router.post("/invoice-payload")
async def build_invoice_payload(
    request: Request,
    db: AsyncSession = Depends(get_db),
    role: str = Depends(require_ops_or_admin),
):
    """Turn verified packing days into the payload templates/invoice.html consumes.

    Body: ``{"pack_dates": ["2026-07-30", "2026-07-31"]}``. Multiple dates is the
    normal case, not an edge case — it is how requirement 9's combined days become
    one invoice, which is the whole reason the aggregation is per-ASIN across days
    rather than per-day.

    Refuses, rather than doing something reasonable-looking, when:

    * any requested day is not `verified` — 400. The owner's approval is what
      gates the GST number, and "all but one day approved" is not approval.
    * any requested day already has an invoice — 409. Invoicing the same boxes
      twice is a GST problem, not a UI annoyance.

    ``fc_code`` and ``shipment_id`` are optional and both come from the owner, not
    from a guess. The FC is the one HE chose when building the upload file (ISK3,
    DED3, BLR4 …), and the shipment ID is what Amazon returned after the upload
    created the shipment. Given the FC code, `get_fc_info` resolves the address, the
    state and — through `get_gstin_for_state` — which of the 15 GSTINs applies and
    therefore whether this is inter-state or intra-state GST. That is the whole
    reason the code is worth carrying: everything else on a tax document follows
    from it.

    Left blank when not supplied, exactly as before. A blank FC is a field the owner
    fills in; a *wrong* FC is the wrong state's GSTIN on a GST document, and nothing
    downstream could detect it.
    """
    body, error = await _json_object(request)
    if error:
        return error
    raw_dates = body.get("pack_dates") or []
    if isinstance(raw_dates, str):
        raw_dates = [raw_dates]

    # De-duplicated but order preserved, so the response lists dates the way the
    # owner selected them.
    pack_dates: list[str] = []
    for value in raw_dates:
        text = str(value).strip()
        if text and text not in pack_dates:
            pack_dates.append(text)

    if not pack_dates:
        return JSONResponse(
            {"error": "Select at least one packed day to invoice."}, status_code=400
        )

    # The FC the owner chose, and Amazon's shipment ID once he has it. Upper-cased
    # because `get_fc_info` keys on the upper-case code and the owner types "isk3"
    # as readily as "ISK3" — a case mismatch would silently fall through to the
    # unknown-FC branch and produce an invoice with no GSTIN.
    fc_code = str(body.get("fc_code") or "").strip().upper()
    shipment_id = str(body.get("shipment_id") or "").strip()

    invalid = [d for d in pack_dates if not _valid_date(d)]
    if invalid:
        return JSONResponse(
            {"error": f"Not a date (YYYY-MM-DD): {', '.join(invalid)}"}, status_code=400
        )

    plan = await repository.get_active_plan(db)
    if plan is None:
        return JSONResponse({"error": "No active plan"}, status_code=404)

    days = []
    for pack_date in pack_dates:
        day = await repository.get_day(db, plan.id, pack_date)
        if day is None:
            return JSONResponse(
                {"error": f"Nothing was packed on {pack_date}."}, status_code=404
            )
        days.append(day)

    # Already invoiced is checked FIRST. Both conditions can be true at once (a
    # shipped day is not `verified` either), and "already on invoice ST/26-27/031"
    # tells the owner what actually happened, where "not verified yet" would send
    # him looking for a verify button on a day that is finished.
    invoiced = [d for d in days if d.invoice_id]
    if invoiced:
        numbers = await _invoice_numbers(db, [d.invoice_id for d in invoiced])
        return JSONResponse(
            {
                "error": "Already invoiced: "
                + ", ".join(
                    f"{d.pack_date} ({numbers.get(d.invoice_id) or '#' + str(d.invoice_id)})"
                    for d in invoiced
                )
                + ". Invoicing the same boxes twice would put two GST documents "
                "against one shipment.",
                "pack_dates": [d.pack_date for d in invoiced],
            },
            status_code=409,
        )

    unverified = [d for d in days if d.status not in logic.INVOICEABLE_STATUSES]
    if unverified:
        return JSONResponse(
            {
                "error": "These days are not verified yet: "
                + ", ".join(f"{d.pack_date} ({d.status})" for d in unverified)
                + ". The owner's verification is what allows a GST invoice to be "
                "raised, so every selected day has to be verified first.",
                "pack_dates": [d.pack_date for d in unverified],
            },
            status_code=400,
        )

    # Units per ASIN across the selected days. This is the aggregation that makes
    # two combined held days into one invoice.
    day_dicts = [
        d
        for d in await repository.load_days_with_entries(db, plan.id)
        if d["pack_date"] in set(pack_dates)
    ]
    units_by_asin = logic.units_by_asin(day_dicts)
    total_cartons = sum(int(d["total_cartons"] or 0) for d in day_dicts)

    # Ordered through load_plan_items so the invoice lines come out product-then-
    # weight, the same order as the screen and the four downloads. An invoice the
    # owner checks against the packed sheet should read down in the same order.
    items = await repository.load_plan_items(db, plan.id)

    lines = []
    for item in items:
        units = int(units_by_asin.get(item.asin, 0))
        if units <= 0:
            continue
        title = _title_for(item)
        # The Products tab is the source of truth for HSN, GST and the purchase rate.
        # Keyed by ASIN rather than by SKU: the merchant SKU is blank on 108 sheet rows,
        # arrives from the uploaded CSV and gets edited by hand, while the ASIN identifies
        # a product for its whole life. Falls back to 1106 at 5% — every F2D product today.
        hsn = await products.tax_for(db, item.asin)
        lines.append(
            {
                "sku": item.fba_sku or "",
                "title": title,
                "short_title": " ".join(title.split()[:10]),
                "asin": item.asin,
                # Blank on purpose: the FNSKU only exists on an Amazon shipment
                # document, and this invoice is being raised before one exists.
                "fnsku": "",
                "quantity": units,
                "hsn_code": hsn["hsn_code"],
                "gst_rate": hsn["gst_rate"],
                "rate": await products.rate_for(db, item.asin, item.fba_sku or ""),
                "unit": "Pcs",
                # Pack size in kg, so the invoice screen can total the shipment weight
                # rather than the owner working it out on paper. Sent per LINE as well
                # as in the total below, because a total nobody can break down is a
                # total nobody can check.
                "weight": float(item.weight or 0),
                "line_weight": logic.line_weight(units, item.weight),
            }
        )

    if not lines:
        return JSONResponse(
            {
                "error": "The selected day(s) have no packed units against the "
                "current plan, so there is nothing to invoice."
            },
            status_code=400,
        )

    missing_rate = [line["sku"] or line["asin"] for line in lines if not line["rate"]]
    missing_sku = [line["asin"] for line in lines if not line["sku"]]

    # Net product weight from units × pack size. Computed here rather than in the
    # browser so the CSV path and this path get the identical number from one function.
    weight = logic.shipment_weight(lines)

    # **What Amazon recorded beats what was typed.** If the shipment was created through
    # this app, the days already carry the FBA id and the destination Amazon actually
    # chose — and those are facts, where the form fields are intentions. Preferring them
    # is what makes the invoice fill itself in after a shipment is created, and it is also
    # the only way the FC on a tax document cannot disagree with where the boxes went.
    #
    # A typed value still wins over nothing, for the manual path where the shipment was
    # made in Seller Central and the app was told about it afterwards.
    for day in days:
        if getattr(day, "shipment_confirmation_id", None):
            shipment_id = shipment_id or day.shipment_confirmation_id
            if getattr(day, "destination_warehouse_id", None):
                fc_code = day.destination_warehouse_id
            break

    # The chosen FC resolved to an address, a state and a GSTIN. Empty dict when no
    # code was supplied, so the invoice screen behaves exactly as it did before.
    fc_info = get_fc_info(fc_code) if fc_code else {}

    warnings = []
    if fc_code and not fc_info.get("recipient_gstin"):
        # An unknown code, or a state we hold no GSTIN for — there are FCs in Madhya
        # Pradesh, Kerala and Andhra Pradesh where we have no registration. Either way
        # `get_fc_info` returns a blank GSTIN rather than failing, so without this the
        # owner gets a GST document with an empty recipient GSTIN and no hint why.
        warnings.append(
            f"{fc_code} did not resolve to a GSTIN"
            + (f" (state: {fc_info.get('state')})" if fc_info.get("state") else "")
            + ". Check the FC code, and confirm we are registered in that state — the "
            "recipient GSTIN and the inter-state/intra-state GST split both depend on it."
        )
    if missing_rate:
        warnings.append(
            f"{len(missing_rate)} line(s) have no purchase rate in the master "
            "pricing and will come through blank — fill them in before saving, or "
            "the taxable value will be wrong."
        )
    if missing_sku:
        warnings.append(
            f"{len(missing_sku)} line(s) have no merchant SKU recorded."
        )
    if weight["unknown"]:
        # Said out loud, because the total is SHORT by however much those lines weigh
        # and the number still looks like a complete answer.
        warnings.append(
            f"{weight['unknown']} line(s) have no pack size recorded, so the "
            f"calculated weight of {weight['total']} kg does not include them. Check "
            "the Net Weight column in the MRP sheet, or type the weight in by hand."
        )

    return JSONResponse(
        {
            "source": "shipment",
            "plan_id": plan.id,
            "pack_dates": pack_dates,
            "metadata": {
                # Amazon's shipment ID, pasted in by the owner after the upload
                # created the shipment. Blank until he has it.
                "shipment_id": shipment_id,
                "name": "",
                # The FC HE chose for this shipment. Everything else on the tax
                # document follows from it: the address, the destination state, and
                # therefore which GSTIN applies and whether GST is inter-state.
                "ship_to": fc_code,
                "recipient_gstin": fc_info.get("recipient_gstin", ""),
                "supplier_gstin": SUPPLIER_GSTIN,
                "warehouse": fc_info,
                "total_skus": len(lines),
                "total_units": sum(line["quantity"] for line in lines),
            },
            "items": lines,
            # Requirement 7's concrete payoff: the cartons ops counted daily
            # prefill the invoice's Boxes field instead of being recounted.
            "boxes": total_cartons,
            # Net product weight, with the per-line working. NET on purpose: cartons,
            # filler and tape are not in the catalogue, so the weighbridge figure will
            # be higher — the invoice screen labels it as net and lets the owner type
            # over it rather than presenting a number that disagrees with the truck.
            "weight": weight,
            "warnings": warnings,
        }
    )


@router.post("/attach-invoice")
async def attach_invoice(
    request: Request,
    db: AsyncSession = Depends(get_db),
    role: str = Depends(require_ops_or_admin),
):
    """Mark days `shipped` and record which invoice covers them.

    Body: ``{"pack_dates": [...], "invoice_id": 42}``.

    NOT under ``/packing/`` — deliberately, and found by a failing test rather
    than foresight. ``POST /packing/{pack_date}`` is declared above, FastAPI
    matches in declaration order, and so ``/packing/attach-invoice`` was being
    parsed as a *date* and rejected with "Date must be YYYY-MM-DD". The damage
    that hides is specific: the attach silently fails, the days stay `verified`
    with no invoice_id, and the double-invoice guard has nothing recorded to fire
    on. A path that cannot collide is worth more than a tidy prefix.

    A separate call rather than a hook inside ``POST /invoice/save`` — that route
    allocates the legally-sequential GST number and has 26 tests pinning the
    sequence, so it is left alone on purpose. The cost of the separation is
    honest and worth stating plainly, because it is a real hole and not a
    theoretical one: if the browser dies between the save and this call, the
    invoice exists while the days still read `verified` with no invoice_id. The
    app then believes they are un-invoiced, and the 409 here cannot help —
    nothing was recorded for it to fire on. A second invoice for the same boxes
    is possible in that window.

    It is accepted anyway, for two reasons. It is recoverable: calling this again
    fixes the record, and the owner is the one clicking through the invoice
    screen, so he sees the number he just raised. And the alternative coupling is
    worse and *not* recoverable — a bug in this bookkeeping rolling back a
    committed invoice would burn a number out of a legally-sequential GST series,
    which cannot be undone by deleting a row.

    Flagged in CLAUDE.md rather than papered over: closing it properly means
    /invoice/save writing the attachment in its own transaction, which is a
    change to the route whose 26 tests guard the GST sequence, and that is not a
    thing to do in the same step as building the bridge.

    Idempotent for the same invoice, so a retried call after a flaky response is
    harmless. Attaching a *different* invoice to an already-invoiced day is
    refused: that is two GST documents against one set of boxes.
    """
    body, error = await _json_object(request)
    if error:
        return error
    raw_dates = body.get("pack_dates") or []
    if isinstance(raw_dates, str):
        raw_dates = [raw_dates]

    try:
        invoice_id = int(body.get("invoice_id"))
    except (TypeError, ValueError):
        return JSONResponse({"error": "invoice_id is required"}, status_code=400)

    pack_dates = [str(d).strip() for d in raw_dates if str(d).strip()]
    if not pack_dates:
        return JSONResponse({"error": "pack_dates is required"}, status_code=400)

    # `plan_id` first, active as the fallback — the same pattern POST /items uses.
    #
    # This route USED to resolve only the active plan, and closing a plan therefore
    # made an invoice unrecordable against its days: the owner's only remedy was
    # editing the database. Close deliberately creates that state (a shipped day with
    # no invoice stays on the plan it shipped from), so the id must be accepted.
    raw_plan_id = body.get("plan_id")
    # Validate plan_id explicitly to avoid an unhandled 500 on a route that writes
    # GST invoice attachments — a generic error gives the owner no idea which field
    # was malformed, and these are legally-sequential tax documents.
    if "plan_id" in body and raw_plan_id is not None:
        try:
            plan_id = int(raw_plan_id)
        except (TypeError, ValueError):
            return JSONResponse(
                {"error": "plan_id must be an integer"}, status_code=400
            )
        plan = await repository.get_plan(db, plan_id)
    else:
        plan = await repository.get_active_plan(db)
    if plan is None:
        return JSONResponse(
            {"error": "No such plan, and no active plan."}, status_code=404
        )

    updated, already = [], []
    for pack_date in pack_dates:
        day = await repository.get_day(db, plan.id, pack_date)
        if day is None:
            return JSONResponse(
                {"error": f"Nothing was packed on {pack_date}."}, status_code=404
            )
        if day.invoice_id and day.invoice_id != invoice_id:
            numbers = await _invoice_numbers(db, [day.invoice_id])
            named = numbers.get(day.invoice_id) or f"#{day.invoice_id}"
            return JSONResponse(
                {
                    "error": f"{pack_date} is already on invoice {named}. "
                    "Two GST invoices must not cover the same boxes.",
                    "invoice_id": day.invoice_id,
                    "invoice_number": named,
                },
                status_code=409,
            )
        if day.invoice_id == invoice_id and day.status == logic.STATUS_SHIPPED:
            already.append(pack_date)
            continue
        day.invoice_id = invoice_id
        day.status = logic.STATUS_SHIPPED
        updated.append(pack_date)

    await db.commit()
    return JSONResponse(
        {
            "status": "attached",
            "invoice_id": invoice_id,
            "updated": updated,
            "already_attached": already,
        }
    )


# ─── One-shot legacy import (admin) ──────────────────────────────────────────

@router.post("/import-legacy")
async def import_legacy(
    request: Request,
    db: AsyncSession = Depends(get_db),
    role: str = Depends(require_admin),
):
    """Import the old shipment_plan.json, mapping day1..day6 to synthetic dates.

    Deliberately a route and not an Alembic migration: file I/O does not belong
    in schema history, and this needs to run at most once, by hand, on a machine
    that happens to still have the file.

    The old format had no real dates — only "day 1" through "day 6" — so the
    imported days are labelled from the file's created_at. They are historical
    record, not something anyone will pack against again.
    """
    if not LEGACY_DATA_FILE.exists():
        return JSONResponse(
            {"error": "No shipment_plan.json found; nothing to import."},
            status_code=404,
        )

    try:
        with open(LEGACY_DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        return JSONResponse({"error": f"Could not read the file: {e}"}, status_code=400)

    legacy_items = data.get("plan") or []
    if not legacy_items:
        return JSONResponse({"error": "The file has no plan rows."}, status_code=400)

    try:
        created = datetime.fromisoformat(str(data.get("created_at")))
    except (TypeError, ValueError):
        created = datetime.utcnow()

    items = [
        {
            "brand": row.get("brand") or "",
            "fba_sku": row.get("fba_sku") or "",
            "asin": row.get("asin") or "",
            "item": row.get("item") or "",
            "weight": row.get("weight") or 0,
            "sales_7d": row.get("sales_7d") or 0,
            "projection": row.get("projection") or 0,
            "fba_stock": row.get("fba_stock") or 0,
            "deficit": row.get("deficit") or 0,
            # Imported verbatim, NOT re-rounded: these are numbers the owner
            # already worked to, and quietly changing them on import would make
            # the imported plan disagree with the shipment already sent.
            "shipment_plan": row.get("shipment_plan") or 0,
            "available": row.get("available") or 0,
            "s": bool(row.get("s")),
            "m": bool(row.get("m")),
            "b": bool(row.get("b")),
        }
        for row in legacy_items
        if row.get("asin")
    ]

    plan = await repository.create_plan(
        db,
        items,
        multiplier=float(data.get("multiplier") or 5),
        label=f"Imported {created:%Y-%m-%d}",
    )

    days_created = 0
    for offset in range(1, 7):
        entries = [
            {"asin": row["asin"], "units": int(row.get(f"day{offset}") or 0)}
            for row in legacy_items
            if row.get("asin") and int(row.get(f"day{offset}") or 0) > 0
        ]
        if not entries:
            continue
        # Synthetic consecutive dates from the file's creation date — the old
        # format only knew "day 1..6", so there is no true date to recover.
        pack_date = date.fromordinal(created.date().toordinal() + offset - 1)
        await repository.save_packing_entries(
            db, plan.id, pack_date.isoformat(), entries, submitted_by=ROLE_ADMIN
        )
        days_created += 1

    return JSONResponse(
        {
            "status": "imported",
            "plan_id": plan.id,
            "items": len(items),
            "days": days_created,
            "note": "Cartons are 0 — the old format never recorded them.",
        }
    )
