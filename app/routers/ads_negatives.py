"""Ads → Optimisation → Search terms: preview and add negatives from overspending search terms.

Spec: `docs/superpowers/specs/2026-10-25-ads-search-term-negatives-design.md`. Independent of the
keyword and SKU rules (`routers.ads`): this never changes a bid, a state or a SKU, and they never add a
negative. **`POST /ads/negatives/apply` writes to Amazon**, so it follows `/ads/apply`'s order: the
server re-runs the plan and takes only rows the plan approves (the client sends keys, nothing else),
the row limit, a live re-read of the ad groups' negatives, the ledger before the wire, then each row's
own outcome.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import permissions
from app.ads import logic, negatives, repository, search_store, spapi_ads
from app.config import get_settings
from app.database import get_db
from app.models import AdsEntity
from app.portfolio.ads import AdsError, AdsNotConfigured
from app.routers.auth import require_area

router = APIRouter(prefix="/ads/negatives")
logger = logging.getLogger(__name__)

PRODUCT_NAMES = {"sp": "Sponsored Products", "sb": "Sponsored Brands"}


def _days(start: str, end: str) -> list[str]:
    first, last = date.fromisoformat(start), date.fromisoformat(end)
    return [(first + timedelta(days=i)).isoformat() for i in range((last - first).days + 1)]


async def _build(db, body) -> tuple[dict | None, JSONResponse | None]:
    """The plan for a request body, or a refusal. ONE function behind preview and apply."""
    from app.routers.ads import _rule_summary, _window

    try:
        start, end = _window(body.get("start"), body.get("end"), body.get("days"))
    except ValueError as exc:
        return None, JSONResponse({"error": str(exc)}, status_code=400)
    conditions = body.get("conditions") or []
    if not conditions:
        return None, JSONResponse({"error": "Add at least one condition."}, status_code=400)
    for condition in conditions:
        problem = logic.condition_error(condition, logic.LEVEL_SEARCH)
        if problem:
            return None, JSONResponse({"error": problem}, status_code=400)

    held = await search_store.search_days_held(db)
    wanted = _days(start, end)
    missing = {p: [d for d in wanted if d not in held.get(p, set())] for p in ("sp", "sb")}
    if missing["sp"]:
        return None, JSONResponse({
            "error": f"Sponsored Products search terms are not stored for {len(missing['sp'])} day(s) "
                     f"of {start}..{end} (from {missing['sp'][0]}). Refresh from Amazon, or pick a "
                     "window inside the days held."}, status_code=409)

    rows = await search_store.sum_search_terms(db, start, end, campaign_ids=body.get("campaign_ids") or None,
                                               ad_group_ids=body.get("ad_group_ids") or None)
    notes = []
    if missing["sb"]:
        # A partly-held SB window would sum short. Left out whole, and said.
        rows = [r for r in rows if r["ad_product"] != "sb"]
        notes.append(f"Sponsored Brands search terms are missing {len(missing['sb'])} day(s) of this "
                     "window, so Sponsored Brands is left out of this preview.")
    groups = {r["ad_group_id"] for r in rows}
    positives = await search_store.group_positives(db, groups, start, end)
    existing = await search_store.load_negatives(db)
    campaign_names = dict((await db.execute(
        select(AdsEntity.entity_id, AdsEntity.name).where(AdsEntity.entity_type == "campaign"))).all())
    guardrails = await repository.load_guardrails(db)
    plan = negatives.plan(rows, conditions, guardrails=guardrails, campaign_names=campaign_names,
                          positives=positives, negatives=existing)
    plan.update({
        "kind": "negatives", "window": [start, end], "notes": notes,
        "rule": _rule_summary(conditions, logic.ACTION_NEGATE, None, level=logic.LEVEL_SEARCH),
        "guardrails": guardrails,
    })
    return plan, None


@router.post("/preview")
async def preview(request: Request, db: AsyncSession = Depends(get_db),
                  grant=Depends(require_area(permissions.ADS))):
    """What a search-term rule WOULD negate. Sends nothing and stores nothing."""
    plan, refusal = await _build(db, await request.json())
    return refusal or plan


@router.post("/apply")
async def apply(request: Request, db: AsyncSession = Depends(get_db),
                grant=Depends(require_area(permissions.ADS))):
    """Add the ticked search terms as negatives in their ad groups. **Writes to Amazon.**

    1. The plan is rebuilt here; only its APPROVED rows whose key the screen sent are kept.
    2. A blocked plan (over `max_rows`) is refused, and Sponsored Brands rows need `confirm_sb`
       because Amazon's SB archive is permanent — there is no undo.
    3. The ad groups' LIVE negatives are re-read; a term already negated is dropped (`already_there`).
    4. The ledger as `pending` (`action="negate"`), BEFORE the request.
    5. Create per writer; a made negative takes Amazon's id and goes into the cache.
    """
    from app.routers.ads import _window_key

    body = await request.json()
    plan, refusal = await _build(db, body)
    if refusal:
        return refusal
    if plan["blocked"]:
        return JSONResponse({"error": plan["blocked"]}, status_code=400)
    keys = set(str(k) for k in body.get("keys") or ())
    wanted = [c for c in plan["changes"] if c["key"] in keys]
    if not wanted:
        return JSONResponse({"error": "None of the ticked search terms is still a candidate — preview "
                                      "again."}, status_code=409)
    if any(c["no_undo"] for c in wanted) and not body.get("confirm_sb"):
        return JSONResponse({"error": "Sponsored Brands negatives cannot be undone. Confirm to add them.",
                             "needs_confirm_sb": True}, status_code=400)
    settings = get_settings()
    if not settings.ads_configured:
        return JSONResponse({"error": "Advertising credentials are not configured."}, status_code=400)

    try:
        async with httpx.AsyncClient(timeout=settings.ads_timeout) as client:
            live = await spapi_ads.fetch_negatives(client, ad_group_ids={c["ad_group_id"] for c in wanted})
            present = {(r["parent_id"], r["entity_type"], r["name"]) for r in live}
            def neg_type(c):
                return search_store.NEG_TARGET if c["kind"] == negatives.KIND_PRODUCT else search_store.NEG_KEYWORD
            already = [c for c in wanted if (c["ad_group_id"], neg_type(c), c["search_term"]) in present]
            to_send = [c for c in wanted if c not in already]
            already_there = [{"key": c["key"], "search_term": c["search_term"]} for c in already]
            if not to_send:
                return {"run_id": None, "applied": 0, "failed": 0, "pending": 0,
                        "already_there": already_there, "results": []}

            changes = [{
                "entity_id": negatives.provisional_id(c["key"]), "negate": True, "writer": c["writer"],
                "ad_product": c["ad_product"], "text": c["search_term"], "search_term": c["search_term"],
                "campaign_id": c["campaign_id"], "ad_group_id": c["ad_group_id"],
                "new_state": logic.STATE_ENABLED, "key": c["key"],
            } for c in to_send]
            run_id = await repository.open_run(db, changes, rule_summary=plan["rule"],
                                               rule_window=_window_key(body))
            results: list[dict] = []
            for writer in negatives.WRITERS:
                rows = [c for c in changes if c["writer"] == writer]
                if rows:
                    results.extend(await spapi_ads.create_negatives(client, rows, writer=writer))

        counts = await repository.record_create_results(
            db, run_id, [{**r, "ad_id": r.get("new_id")} for r in results])
        made = {r["entity_id"]: r["new_id"] for r in results if r.get("ok") and r.get("new_id")}
        await search_store.add_negatives_to_cache(db, [{
            "entity_type": neg_type(c), "ad_product": c["ad_product"], "entity_id": made[pid],
            "parent_id": c["ad_group_id"], "campaign_id": c["campaign_id"], "name": c["search_term"]}
            for c in to_send if (pid := negatives.provisional_id(c["key"])) in made])
        try:
            from app.routers.ads import _rule_summary
            await repository.mark_rules_run(db, [
                r["id"] for r in await repository.load_rules(db)
                if _rule_summary(r["conditions"], r["action"], r["amount"],
                                 level=r.get("level") or logic.LEVEL_KEYWORD) == plan["rule"]])
        except Exception:  # noqa: BLE001 - bookkeeping only
            logger.warning("ads: could not stamp the saved rule's last run", exc_info=True)
    except AdsNotConfigured as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except AdsError as exc:
        logger.warning("ads negatives apply failed: %s", exc)
        return JSONResponse({"error": str(exc)}, status_code=502)

    by_id = {c["entity_id"]: c for c in changes}
    logger.info("ads: negatives run %s %s", run_id, counts)
    return {"run_id": run_id, **counts, "already_there": already_there,
            "results": [{**r, "search_term": by_id.get(r["entity_id"], {}).get("search_term", "")}
                        for r in results]}
