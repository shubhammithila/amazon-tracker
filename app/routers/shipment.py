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
from app.routers.auth import ROLE_ADMIN, require_admin, require_ops_or_admin
from app.shipment import documents, logic, repository

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
    return {
        "asin": item.asin,
        "fba_sku": item.fba_sku or "",
        "brand": item.brand or "",
        "item": item.item or "",
        "weight": float(item.weight or 0),
        "sales_7d": int(item.sales_7d or 0),
        "projection": int(item.projection or 0),
        "fba_stock": int(item.fba_stock or 0),
        "deficit": int(item.deficit or 0),
        "shipment_plan": planned,
        "available": int(item.available or 0),
        "s": bool(item.s),
        "m": bool(item.m),
        "b": bool(item.b),
        "packed": packed,
        "shippable": shippable,
        "remaining": logic.remaining_for(planned, packed),
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

    items: list[dict] = []
    for asin, info in FAMILIES.items():
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

    plan = await repository.create_plan(db, items, multiplier=multiplier)
    stored = await repository.load_plan_items(db, plan.id)
    missing_sku = await repository.count_items_missing_sku(db, plan.id)

    payload = _plan_payload(plan, stored, [])
    payload["missing_sku_count"] = missing_sku
    payload["abandoned_holds"] = abandoned_holds

    warnings = []
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

    day = await repository.save_packing_entries(
        db, plan.id, pack_date, entries, submitted_by=role
    )
    return JSONResponse(
        {
            "status": "saved",
            "pack_date": day.pack_date,
            "day_status": day.status,
            "total_units": int(day.total_units or 0),
            "total_cartons": int(day.total_cartons or 0),
        }
    )


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


@router.get("/download/packing-plan.xlsx")
async def download_packing_plan_xlsx(
    request: Request,
    db: AsyncSession = Depends(get_db),
    role: str = Depends(require_admin),
):
    """The full plan as Excel — requirement 3."""
    loaded = await _document_rows(db)
    if loaded is None:
        return _no_plan()
    plan, rows, _days = loaded
    return _attachment(
        documents.build_packing_plan_xlsx(plan, rows),
        f"packing-plan-{date.today().isoformat()}.xlsx",
        XLSX_TYPE,
    )


@router.get("/download/packing-plan.pdf")
async def download_packing_plan_pdf(
    request: Request,
    db: AsyncSession = Depends(get_db),
    role: str = Depends(require_admin),
):
    """The full plan as PDF — requirement 3 asked for both formats."""
    loaded = await _document_rows(db)
    if loaded is None:
        return _no_plan()
    plan, rows, _days = loaded
    return _attachment(
        documents.build_packing_plan_pdf(plan, rows),
        f"packing-plan-{date.today().isoformat()}.pdf",
        "application/pdf",
    )


@router.get("/download/remaining.pdf")
async def download_remaining_pdf(
    request: Request,
    pack_date: str | None = None,
    db: AsyncSession = Depends(get_db),
    role: str = Depends(require_ops_or_admin),
):
    """The morning clipboard sheet — requirement 5.

    Open to ops, unlike the other downloads: this is the one document the packer
    actually needs, and making him ask the owner for it every morning would
    defeat the point of giving him his own screen.
    """
    if pack_date is not None and not _valid_date(pack_date):
        return JSONResponse({"error": "Date must be YYYY-MM-DD"}, status_code=400)

    loaded = await _document_rows(db)
    if loaded is None:
        return _no_plan()
    plan, rows, _days = loaded
    stamp = pack_date or date.today().isoformat()
    return _attachment(
        documents.build_remaining_pdf(plan, rows, pack_date=stamp),
        f"still-to-pack-{stamp}.pdf",
        "application/pdf",
    )


@router.get("/download/packed.xlsx")
async def download_packed_xlsx(
    request: Request,
    db: AsyncSession = Depends(get_db),
    role: str = Depends(require_admin),
):
    """Daily packed units and cartons — requirements 6 and 7.

    Admin only. This is the owner's input for building the actual shipment, and
    it exposes every day including held ones.
    """
    loaded = await _document_rows(db)
    if loaded is None:
        return _no_plan()
    plan, rows, days = loaded
    return _attachment(
        documents.build_packed_xlsx(plan, rows, days),
        f"packed-daily-{date.today().isoformat()}.xlsx",
        XLSX_TYPE,
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
