"""Ads → SKU coverage: the SKUs each SP ad group is missing, and a one-click Add.

Spec: `docs/superpowers/specs/2026-10-24-ads-sku-coverage-design.md`. The check (`ads.coverage`) reads
only stored rows. **`POST /ads/coverage/add` is the one route here that changes Amazon**, and it
follows `/ads/apply`'s order: the server decides what is a gap (never the client), the row limit, a
live re-read, the ledger before the wire, then each row's own outcome.
"""
from __future__ import annotations

import logging

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app import ist, permissions
from app.ads import coverage, listings, logic, repository, spapi_ads
from app.config import get_settings
from app.database import async_session, get_db
from app.portfolio.ads import AdsError, AdsNotConfigured
from app.routers.auth import require_area
from app.shipment import documents

router = APIRouter(prefix="/ads")
logger = logging.getLogger(__name__)

CHANNEL_LABELS = logic.CHANNEL_LABELS
KIND_LABELS = {coverage.KIND_SKU: "another SKU of a size already in the group",
               coverage.KIND_SIZE: "a size not in the group"}


@router.get("/coverage")
async def coverage_view(db: AsyncSession = Depends(get_db),
                        grant=Depends(require_area(permissions.ADS))):
    result = await coverage.build(db)
    result["channel_labels"] = CHANNEL_LABELS
    result["kind_labels"] = KIND_LABELS
    return result


@router.get("/coverage.xlsx")
async def coverage_xlsx(db: AsyncSession = Depends(get_db),
                        grant=Depends(require_area(permissions.ADS))):
    """The missing SKUs (not the ignored ones), one row each, as on screen."""
    result = await coverage.build(db)
    rows = [[m["campaign"], m["ad_group"], m["product"], m["size"], m["sku"], m["asin"],
             CHANNEL_LABELS.get(m["channel"], m["channel"]), m["listing_status"],
             KIND_LABELS.get(m["kind"], m["kind"])]
            for m in coverage.gaps_by_key(result).values()]
    totals = result["totals"]
    stream = documents.build_portfolio_xlsx(
        "SKU coverage",
        f"{totals['missing']} SKU(s) missing across {totals['groups_with_gaps']} of "
        f"{totals['groups_checked']} SP ad groups · {ist.today().isoformat()}",
        ["Campaign", "Ad group", "Product", "Size", "SKU", "ASIN", "Channel", "Listing status", "Gap"],
        rows, [26, 24, 30, 9, 30, 13, 10, 12, 34],
    )
    return StreamingResponse(
        stream, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition":
                 f'attachment; filename="sku-coverage-{ist.today().isoformat()}.xlsx"'})


@router.post("/coverage/listings")
async def reread_listings(grant=Depends(require_area(permissions.ADS))):
    """Read the account's listings now (~1 minute). Never raises; the result says what happened."""
    async with httpx.AsyncClient(timeout=120) as client:
        status = await listings.refresh(async_session, client)
    return {"listings": status}


@router.post("/coverage/ignore")
async def set_ignore(request: Request, db: AsyncSession = Depends(get_db),
                     grant=Depends(require_area(permissions.ADS))):
    body = await request.json()
    group = str(body.get("ad_group_id") or "").strip()
    key = str(body.get("product_key") or "").strip()
    if not group or not key:
        return JSONResponse({"error": "An ad group and a product are needed."}, status_code=400)
    await repository.set_coverage_ignore(db, group, key, bool(body.get("ignored", True)))
    return {"ok": True}


@router.post("/coverage/add")
async def add_skus(request: Request, db: AsyncSession = Depends(get_db),
                   grant=Depends(require_area(permissions.ADS))):
    """Create the ticked missing SKUs as ENABLED product ads in their ad groups.

    1. **Only CURRENT gaps, by the server's own check.** The screen sends `{ad_group_id, sku}` pairs
       and nothing else; campaign, ASIN and product come from `coverage.build`. A pair that is no
       longer a gap (added since, ignored, or never one) is not sent and is named in `not_a_gap`.
    2. The `max_rows` limit.
    3. **Re-read the ad groups' LIVE product ads**: a SKU Amazon already holds there (any state but
       archived) is dropped and named in `already_there` — never created twice.
    4. The ledger as `pending` (`action="create"`), BEFORE the request.
    5. `POST /sp/productAds`; each row's outcome; a made ad takes Amazon's `adId`, and is put into the
       cached list so the panel stops naming it at once. Undo pauses them.
    """
    body = await request.json()
    asked = [(str(r.get("ad_group_id") or ""), str(r.get("sku") or ""))
             for r in body.get("rows") or [] if isinstance(r, dict)]
    asked = list(dict.fromkeys(k for k in asked if all(k)))
    if not asked:
        return JSONResponse({"error": "No SKUs were ticked."}, status_code=400)

    gaps = coverage.gaps_by_key(await coverage.build(db))
    wanted = [gaps[k] for k in asked if k in gaps]
    not_a_gap = [{"ad_group_id": g, "sku": s} for g, s in asked if (g, s) not in gaps]
    if not wanted:
        return JSONResponse({"error": "None of those SKUs is missing any more — reload the list.",
                             "not_a_gap": not_a_gap}, status_code=409)

    guardrails = await repository.load_guardrails(db)
    if len(wanted) > guardrails["max_rows"]:
        return JSONResponse(
            {"error": f"{len(wanted)} SKUs exceeds the {guardrails['max_rows']}-row limit."},
            status_code=400)

    settings = get_settings()
    if not settings.ads_configured:
        return JSONResponse({"error": "Advertising credentials are not configured."}, status_code=400)

    try:
        async with httpx.AsyncClient(timeout=settings.ads_timeout) as client:
            live = await spapi_ads.fetch_product_ads(
                client, ad_group_ids={w["ad_group_id"] for w in wanted}, states=())
            present = {(str(a["parent_id"]), str(a.get("sku") or a.get("name") or ""))
                       for a in live if str(a.get("state") or "").upper() != coverage.ARCHIVED}
            already = [w for w in wanted if (w["ad_group_id"], w["sku"]) in present]
            to_send = [w for w in wanted if (w["ad_group_id"], w["sku"]) not in present]
            if already:
                await repository.add_product_ads_to_cache(db, [
                    a for a in live if (str(a["parent_id"]), str(a.get("sku") or a.get("name") or ""))
                    in {(w["ad_group_id"], w["sku"]) for w in already}])
            already_there = [{"ad_group_id": w["ad_group_id"], "sku": w["sku"]} for w in already]
            if not to_send:
                return {"run_id": None, "applied": 0, "failed": 0, "pending": 0,
                        "already_there": already_there, "not_a_gap": not_a_gap, "results": []}

            changes = [{
                "entity_id": coverage.provisional_id(w["ad_group_id"], w["sku"]),
                "create": True, "writer": logic.WRITER_PRODUCT_AD, "ad_product": logic.AD_PRODUCT_SP,
                "text": w["sku"], "campaign_id": w["campaign_id"], "ad_group_id": w["ad_group_id"],
                "new_state": logic.STATE_ENABLED, "sku": w["sku"], "asin": w["asin"],
            } for w in to_send]
            run_id = await repository.open_run(
                db, changes, rule_summary=f"SKU coverage: add {len(changes)} SKU(s)")
            results = await spapi_ads.create_product_ads(client, changes)

        counts = await repository.record_create_results(db, run_id, results)
        made = {r["entity_id"]: r["ad_id"] for r in results if r.get("ok") and r.get("ad_id")}
        await repository.add_product_ads_to_cache(db, [
            {"entity_id": made[c["entity_id"]], "parent_id": c["ad_group_id"],
             "campaign_id": c["campaign_id"], "name": c["sku"], "asin": c["asin"],
             "state": logic.STATE_ENABLED}
            for c in changes if c["entity_id"] in made])
    except AdsNotConfigured as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except AdsError as exc:
        logger.warning("ads coverage add failed: %s", exc)
        return JSONResponse({"error": str(exc)}, status_code=502)

    by_id = {c["entity_id"]: c for c in changes}
    logger.info("ads: coverage run %s %s", run_id, counts)
    return {"run_id": run_id, **counts, "already_there": already_there, "not_a_gap": not_a_gap,
            "results": [{**r, "sku": by_id.get(r["entity_id"], {}).get("sku", "")} for r in results]}
