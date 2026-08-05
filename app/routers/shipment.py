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
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.invoice.company_data import SUPPLIER_GSTIN
from app.invoice.hsn_codes import lookup_hsn
from app.invoice.parser import get_purchase_rate
from app.routers.auth import ROLE_ADMIN, require_admin, require_ops_or_admin
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

    # Column T of the product master sheet: only products the owner still sells
    # may reach a plan. A discontinued SKU otherwise lands on the packer's morning
    # sheet and in the Amazon upload — a wasted trip and a rejected line.
    #
    # Never raises: on a fetch failure this returns the last good list (or an
    # empty one) plus a warning, because a Google outage must not stop the owner
    # building a plan.
    active_flags, sheet_warning = await catalogue.load_active_flags()

    items: list[dict] = []
    skipped_inactive = 0
    for asin, info in FAMILIES.items():
        if not catalogue.is_active(active_flags, asin):
            skipped_inactive += 1
            continue

        units_sold = sales.get(asin, 0)
        fba_stock = stock.get(asin, 0)
        projection = int(units_sold * multiplier)
        deficit = projection - fba_stock

        items.append({
            "brand": "MF" if info.get("brand") == "Mithila Foods" else "HF",
            "fba_sku": sku_map.get(asin, ""),
            "asin": asin,
            "item": info.get("parent_product") or "",
            "weight": info.get("weight") or 0,
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

    # A DRAFT, and the active plan is left completely alone. The owner removes
    # rows, fixes quantities and fills missing SKUs before the warehouse ever
    # sees it; POST /plan/{id}/finalise is what promotes it. Any previous draft is
    # discarded first so there is only ever one to work on.
    await repository.delete_draft_plans(db)
    plan = await repository.create_plan(
        db, items, multiplier=multiplier, status=repository.STATUS_DRAFT
    )
    stored = await repository.load_plan_items(db, plan.id)
    missing_sku = await repository.count_items_missing_sku(db, plan.id)

    payload = _plan_payload(plan, stored, [])
    payload["missing_sku_count"] = missing_sku
    payload["abandoned_holds"] = abandoned_holds
    payload["skipped_inactive"] = skipped_inactive

    warnings = []
    # First, because it explains the row count. Without it the owner sees 117 rows
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
    if warnings:
        payload["warning"] = " ".join(warnings)
    return JSONResponse(payload)


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
    body = await request.json()
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
    body = await request.json()
    plan = await repository.update_thresholds(
        db, plan_id, body.get("min_cartons"), body.get("min_units")
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
    payload = _plan_payload(plan, items, [])
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
    body = await request.json()
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
    body = await request.json()
    changed = await repository.set_categories(db, body.get("categories") or {})
    return JSONResponse({"status": "saved", "changed": changed})


# ─── Daily packing (ops + admin) ─────────────────────────────────────────────

def _valid_date(value: str) -> bool:
    try:
        date.fromisoformat(value)
        return True
    except (TypeError, ValueError):
        return False


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
                "cartons": int(mine.get("cartons") or 0),
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
    """Upsert units and cartons for a date. The only write ops performs.

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

    body = await request.json()
    entries = body.get("entries") or []

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
        asin = str((raw or {}).get("asin") or "").strip()
        if asin and asin not in included:
            dropped.append(asin)
            continue
        kept.append(raw)

    day = await repository.save_packing_entries(
        db, plan.id, pack_date, kept, submitted_by=role
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

async def _document_rows(db: AsyncSession):
    """(plan, item dicts in canonical order, days) or None if there is no plan."""
    plan = await repository.get_active_plan(db)
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

    loaded = await _document_rows(db)
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
    db: AsyncSession = Depends(get_db),
    role: str = Depends(require_admin),
):
    """What was actually packed, over a date or a range.

    Both dates blank means every packing day on the plan, which is the safe
    default: a blank field should not silently narrow a report.

    Units AND cartons, because the carton count is what prefills the invoice's
    Boxes field — a units-only sheet would send the owner back to the screen to
    read the number he just downloaded a report about.
    """
    if fmt not in ("xlsx", "pdf"):
        return _bad_format(fmt)
    for value in (date_from, date_to):
        if value and not _valid_date(value):
            return JSONResponse({"error": "Dates must be YYYY-MM-DD"}, status_code=400)

    loaded = await _document_rows(db)
    if loaded is None:
        return _no_plan()
    plan, rows, days = loaded

    chosen = [
        d for d in days
        if (not date_from or d["pack_date"] >= date_from)
        and (not date_to or d["pack_date"] <= date_to)
    ]
    units = logic.units_by_asin(chosen)
    cartons: dict[str, int] = {}
    for day in chosen:
        for entry in day.get("entries") or []:
            asin = entry.get("asin") or ""
            if asin:
                cartons[asin] = cartons.get(asin, 0) + int(entry.get("cartons") or 0)

    lines = []
    for item in rows:
        packed_units = int(units.get(item["asin"], 0))
        packed_cartons = int(cartons.get(item["asin"], 0))
        if packed_units <= 0 and packed_cartons <= 0:
            continue
        lines.append(
            documents._identity_cells(item) + [packed_units, packed_cartons]
        )

    span = (
        f"{date_from or 'start'} to {date_to or 'today'}"
        if (date_from or date_to)
        else f"all {len(chosen)} packing day(s)"
    )
    stamp = date_from or date.today().isoformat()
    return _document(
        fmt,
        "Packed",
        f"{plan.get('label') or 'Plan'} · {span}",
        documents.IDENTITY_HEADERS + ["Units", "Cartons"],
        lines,
        documents.IDENTITY_WIDTHS + [12, 12],
        f"packed-{stamp}",
    )


@router.get("/download/remaining.{fmt}")
async def download_remaining(
    fmt: str,
    request: Request,
    pack_date: str | None = None,
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

    loaded = await _document_rows(db)
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
    db: AsyncSession = Depends(get_db),
    role: str = Depends(require_admin),
):
    """The Amazon upload sheet.

    An unknown `mode` is rejected rather than quietly treated as `remaining`. The
    three modes produce genuinely different quantities, and a typo silently
    yielding a plausible-looking file is how the wrong numbers get uploaded to
    Amazon.
    """
    if mode not in ("remaining", "all", "verified"):
        return JSONResponse(
            {"error": "mode must be remaining, all or verified"}, status_code=400
        )

    loaded = await _document_rows(db)
    if loaded is None:
        return _no_plan()
    _plan, rows, days = loaded
    return _attachment(
        documents.build_shipment_file_xlsx(rows, mode=mode, days=days),
        f"shipment-{mode}-{date.today().isoformat()}.xlsx",
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

    `shipment_id`, `fc_code` and `place_of_supply` are deliberately left blank:
    those come from Amazon *after* the shipment exists, and guessing them would
    put a wrong FC on a tax document.
    """
    body = await request.json()
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
        return JSONResponse(
            {
                "error": "Already invoiced: "
                + ", ".join(f"{d.pack_date} (invoice #{d.invoice_id})" for d in invoiced)
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
        # Reuse the invoice module's own lookups rather than reimplementing them,
        # so a rate or HSN correction made through the invoice screen applies here
        # too. lookup_hsn keys on the SKU, which is why fba_sku is persisted.
        hsn = lookup_hsn(title, sku=item.fba_sku or "")
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
                "rate": get_purchase_rate(item.fba_sku or "", item.asin),
                "unit": "Pcs",
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

    warnings = []
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

    return JSONResponse(
        {
            "source": "shipment",
            "plan_id": plan.id,
            "pack_dates": pack_dates,
            "metadata": {
                # Blank: these are Amazon's, and only exist once the shipment does.
                "shipment_id": "",
                "name": "",
                "ship_to": "",
                "recipient_gstin": "",
                "supplier_gstin": SUPPLIER_GSTIN,
                "warehouse": {},
                "total_skus": len(lines),
                "total_units": sum(line["quantity"] for line in lines),
            },
            "items": lines,
            # Requirement 7's concrete payoff: the cartons ops counted daily
            # prefill the invoice's Boxes field instead of being recounted.
            "boxes": total_cartons,
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
    body = await request.json()
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

    plan = await repository.get_active_plan(db)
    if plan is None:
        return JSONResponse({"error": "No active plan"}, status_code=404)

    updated, already = [], []
    for pack_date in pack_dates:
        day = await repository.get_day(db, plan.id, pack_date)
        if day is None:
            return JSONResponse(
                {"error": f"Nothing was packed on {pack_date}."}, status_code=404
            )
        if day.invoice_id and day.invoice_id != invoice_id:
            return JSONResponse(
                {
                    "error": f"{pack_date} is already on invoice #{day.invoice_id}. "
                    "Two GST invoices must not cover the same boxes."
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
            {"asin": row["asin"], "units": int(row.get(f"day{offset}") or 0), "cartons": 0}
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
