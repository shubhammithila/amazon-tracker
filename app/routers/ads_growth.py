"""Ads → Keyword growth: find relevant keywords and competitor targets per ad group, and add them.

Spec: `docs/superpowers/specs/2026-10-26-ads-keyword-growth-design.md`. `POST /ads/growth/find` reads
(Amazon's suggestions, our search terms, amazon.in autocomplete, the product page) and asks Claude which
are relevant; it changes nothing at Amazon. **`POST /ads/growth/add` writes to Amazon**, in
`/ads/coverage/add`'s order: the server rebuilds the list (only KEPT stored rows whose key was sent), the
row and bid limits, a live re-read of the ad group, the ledger before the wire, then each row's outcome.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import permissions
from app.ads import growth, growth_judge, logic, pages, progress, repository, search_store, spapi_ads
from app.config import get_settings
from app.database import get_db
from app.models import AdsEntity, AdsPerformanceDaily, GrowthSuggestion
from app.portfolio.ads import AdsError, AdsNotConfigured
from app.routers.auth import require_area

router = APIRouter(prefix="/ads/growth")
logger = logging.getLogger(__name__)

MAX_KEYWORD_CANDIDATES = 300
MAX_PRODUCT_CANDIDATES = 60
SB_ASINS_CACHE_SECONDS = 600
_sb_asins_cache: dict = {"at": 0.0, "map": None}


async def _sb_ad_asins() -> dict[str, list[str]]:
    """`{ad_group_id: [asin]}` from Amazon's SB ad list (`creative.asins`), held 10 minutes. `{}` when
    Amazon cannot be reached: the SB cards then say they have no products to judge against."""
    if _sb_asins_cache["map"] is not None and time.time() - _sb_asins_cache["at"] < SB_ASINS_CACHE_SECONDS:
        return _sb_asins_cache["map"]
    from app.portfolio import ads as pf_ads
    try:
        async with httpx.AsyncClient(timeout=get_settings().ads_timeout) as client:
            found = await pf_ads.fetch_sb_ad_asins(client)
    except Exception as exc:  # noqa: BLE001 - SB products are optional on the card list
        logger.warning("ads growth: SB ad ASINs could not be read: %s", exc)
        return _sb_asins_cache["map"] or {}
    _sb_asins_cache.update(at=time.time(), map=found)
    return found


async def _cards(db: AsyncSession, *, with_sb_asins: bool = True) -> list[dict]:
    """Every ad group card. ``with_sb_asins=False`` skips the live SB ad list, so the campaign list —
    the page's first screen — reads only our database and answers at once."""
    ents = (await db.execute(select(
        AdsEntity.entity_type, AdsEntity.entity_id, AdsEntity.parent_id, AdsEntity.name, AdsEntity.state,
        AdsEntity.ad_product, AdsEntity.asin,
    ).where(AdsEntity.entity_type.in_(("campaign", "ad_group", "product_ad"))))).all()
    campaigns, groups, asins = {}, {}, {}
    for etype, eid, parent, name, state, product, asin in ents:
        if etype == "campaign":
            campaigns[eid] = {"name": name, "state": state, "ad_product": product}
        elif etype == "ad_group":
            groups[eid] = {"name": name, "state": state, "campaign_id": parent, "ad_product": product}
        elif asin and str(state or "").upper() != "ARCHIVED":
            asins.setdefault(str(parent), []).append(asin)
    if with_sb_asins:
        for group_id, sb_asins in (await _sb_ad_asins()).items():
            asins.setdefault(str(group_id), []).extend(sb_asins)
    kinds: dict[str, set] = {}
    for group_id, match in (await db.execute(
            select(AdsPerformanceDaily.ad_group_id, AdsPerformanceDaily.match_type).distinct())).all():
        kinds.setdefault(str(group_id), set()).add(match)
    return growth.cards(groups, campaigns, {g: growth.group_kind(m) for g, m in kinds.items()}, asins)


def _suggestion_out(row: GrowthSuggestion) -> dict:
    return {"key": row.key, "term": row.term, "kind": row.kind, "keep": bool(row.keep),
            "reason": row.reason or "", "sources": json.loads(row.sources_json or "[]"),
            "figures": json.loads(row.figures_json or "{}"), "bids": json.loads(row.bids_json or "{}"),
            "found_at": row.found_at.isoformat() if row.found_at else ""}


async def _stored(db: AsyncSession, group_ids=None) -> dict[str, list[dict]]:
    query = select(GrowthSuggestion)
    if group_ids is not None:
        query = query.where(GrowthSuggestion.ad_group_id.in_(list(group_ids)))
    out: dict[str, list[dict]] = {}
    for row in (await db.execute(query)).scalars().all():
        out.setdefault(row.ad_group_id, []).append(_suggestion_out(row))
    for rows in out.values():
        rows.sort(key=lambda r: (not r["keep"], -int(r["figures"].get("orders") or 0),
                                 -float(r["figures"].get("spend") or 0), r["term"]))
    return out


async def _counts(db: AsyncSession) -> dict[str, dict]:
    """`{ad_group_id: {"kept", "found", "found_at"}}` from the stored suggestions, in one query."""
    from sqlalchemy import Integer, cast, func
    out = {}
    for group, kept, found, at in (await db.execute(select(
            GrowthSuggestion.ad_group_id, func.sum(cast(GrowthSuggestion.keep, Integer)), func.count(GrowthSuggestion.id),
            func.max(GrowthSuggestion.found_at)).group_by(GrowthSuggestion.ad_group_id))).all():
        out[group] = {"kept": int(kept or 0), "found": int(found or 0),
                      "found_at": at.isoformat() if at else ""}
    return out


@router.get("/campaigns")
async def campaigns_view(db: AsyncSession = Depends(get_db), grant=Depends(require_area(permissions.ADS))):
    """The first screen: one row per campaign with its ad groups counted. Database only — no Amazon
    call — so it answers at once."""
    cards = await _cards(db, with_sb_asins=False)
    counts = await _counts(db)
    campaigns: dict[str, dict] = {}
    for card in cards:
        c = campaigns.setdefault(card["campaign_id"], {
            "campaign_id": card["campaign_id"], "campaign_name": card["campaign_name"],
            "ad_product": card["ad_product"], "keyword_groups": 0, "product_groups": 0,
            "found_groups": 0, "kept": 0, "last_found": ""})
        c["keyword_groups" if card["kind"] == growth.KIND_KEYWORD else "product_groups"] += 1
        got = counts.get(card["ad_group_id"])
        if got:
            c["found_groups"] += 1
            c["kept"] += got["kept"]
            c["last_found"] = max(c["last_found"], got["found_at"])
    return {"campaigns": sorted(campaigns.values(), key=lambda c: c["campaign_name"].lower())}


@router.get("/campaign/{campaign_id}")
async def campaign_view(campaign_id: str, db: AsyncSession = Depends(get_db),
                        grant=Depends(require_area(permissions.ADS))):
    """One campaign's ad groups, each with its stored suggestions. The live SB ad list (which ASINs
    an SB ad group advertises) is read only for an SB campaign."""
    product = (await db.execute(select(AdsEntity.ad_product).where(
        AdsEntity.entity_type == "campaign", AdsEntity.entity_id == campaign_id))).scalar()
    cards = [c for c in await _cards(db, with_sb_asins=(product or "sp") == "sb")
             if c["campaign_id"] == campaign_id]
    if not cards:
        return JSONResponse({"error": "That campaign has no ad group offered here."}, status_code=404)
    stored = await _stored(db, [c["ad_group_id"] for c in cards])
    for card in cards:
        card["suggestions"] = stored.get(card["ad_group_id"], [])
    return {"campaign": {"campaign_id": campaign_id, "campaign_name": cards[0]["campaign_name"],
                         "ad_product": cards[0]["ad_product"]},
            "groups": cards, "source_labels": growth.SOURCE_LABELS}


@router.post("/keep")
async def set_keep(request: Request, db: AsyncSession = Depends(get_db),
                   grant=Depends(require_area(permissions.ADS))):
    """Move suggestions between Relevant and Left out — the owner overruling the check. Stored on the
    suggestion AND as the verdict for this ad group's products, so the next Generate keeps it."""
    from app.models import GrowthVerdict

    body = await request.json()
    group_id = str(body.get("ad_group_id") or "")
    keys = {str(k) for k in body.get("keys") or ()}
    keep = bool(body.get("keep", True))
    rows = (await db.execute(select(GrowthSuggestion).where(GrowthSuggestion.ad_group_id == group_id,
                                                            GrowthSuggestion.key.in_(list(keys))))).scalars().all()
    if not keys or len(rows) != len(keys):
        return JSONResponse({"error": "Some of those rows are not current suggestions — generate again."},
                            status_code=409)
    reason = growth.MOVED_KEEP if keep else growth.MOVED_DROP
    for row in rows:
        row.keep, row.reason = keep, reason
    product = (await db.execute(select(AdsEntity.ad_product).where(
        AdsEntity.entity_type == "ad_group", AdsEntity.entity_id == group_id))).scalar()
    card = next((c for c in await _cards(db, with_sb_asins=(product or "sp") == "sb")
                 if c["ad_group_id"] == group_id), None)
    if card and card["asins"]:
        keys_by_asin, _catalogue = await _product_keys(db, card["asins"])
        representatives: dict[str, str] = {}
        for asin in card["asins"]:
            representatives.setdefault(keys_by_asin[asin], asin)
        pkey = growth_judge.product_key(list(representatives))
        now = datetime.utcnow()
        terms = [r.term for r in rows]
        existing = {v.term: v for v in (await db.execute(select(GrowthVerdict).where(
            GrowthVerdict.product_key == pkey, GrowthVerdict.kind == rows[0].kind,
            GrowthVerdict.term.in_(terms)))).scalars().all()}
        for row in rows:
            verdict = existing.get(row.term) or GrowthVerdict(product_key=pkey, kind=row.kind, term=row.term)
            verdict.keep, verdict.reason, verdict.judged_at = keep, reason, now
            if row.term not in existing:
                db.add(verdict)
    await db.commit()
    return {"ad_group_id": group_id, "moved": len(rows), "keep": keep,
            "suggestions": (await _stored(db, [group_id])).get(group_id, [])}


async def _product_keys(db, asins: list[str]) -> dict[str, str]:
    from app.portfolio import repository as pf_repo
    from app.shipment import catalogue as catalogue_mod
    catalogue, _w, _s = await catalogue_mod.load_catalogue()
    flav = await pf_repo.flavour_keys(db, catalogue)
    parent = await pf_repo.parent_of_children(db)
    return {a: flav.get(a) or parent.get(a) or a for a in asins}, catalogue


@router.post("/find")
async def find(request: Request, db: AsyncSession = Depends(get_db),
               grant=Depends(require_area(permissions.ADS))):
    """Work out one ad group's suggestions. Reads Amazon and asks Claude; writes only our own rows."""
    from app.routers.ads import _window

    body = await request.json()
    group_id = str(body.get("ad_group_id") or "")
    product = (await db.execute(select(AdsEntity.ad_product).where(
        AdsEntity.entity_type == "ad_group", AdsEntity.entity_id == group_id))).scalar()
    card = next((c for c in await _cards(db, with_sb_asins=(product or "sp") == "sb")
                 if c["ad_group_id"] == group_id), None)
    if card is None:
        return JSONResponse({"error": "That ad group is not offered here (paused, automated, or auto "
                                      "targeting)."}, status_code=404)
    if not card["asins"]:
        return JSONResponse({"error": "No product is known for this ad group yet, so there is nothing to "
                                      "judge keywords against."}, status_code=409)
    try:
        start, end = _window(body.get("start"), body.get("end"), body.get("days") or 30)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    settings = get_settings()
    if not settings.ads_configured:
        return JSONResponse({"error": "Advertising credentials are not configured."}, status_code=400)

    kind = card["kind"]
    keys, catalogue = await _product_keys(db, card["asins"])
    representatives: dict[str, str] = {}
    for asin in card["asins"]:
        representatives.setdefault(keys[asin], asin)
    our_pages = await pages.load_pages(db, representatives.values())
    if not our_pages:
        return JSONResponse({"error": "The product page could not be read from Amazon; try again later."},
                            status_code=502)
    page_text = "\n\n".join(pages.page_text(p) for p in our_pages.values())[:9000]

    sold_rows = await search_store.sum_search_terms(db, start, end, ad_group_ids=[group_id])
    sold = [{"term": r["search_term"], "orders": r["orders"], "clicks": r["clicks"], "spend": r["spend"],
             "sales": r["sales"]} for r in sold_rows if r["orders"] > 0]
    sources: dict[str, list] = {growth.SRC_SOLD: sold}
    try:
        async with httpx.AsyncClient(timeout=settings.ads_timeout) as client:
            if kind == growth.KIND_KEYWORD:
                if card["ad_product"] == "sb":
                    sources[growth.SRC_AMAZON] = await spapi_ads.fetch_sb_keyword_suggestions(client, card["asins"])
                else:
                    sources[growth.SRC_AMAZON] = await spapi_ads.fetch_sp_keyword_suggestions(client, card["asins"])
                prefixes = sorted({str((catalogue.get(a) or {}).get("name") or "").strip().lower()
                                   for a in card["asins"]} - {""})[:3]
                completions = []
                for prefix in prefixes:
                    completions += [{"term": t} for t in await spapi_ads.fetch_autocomplete(client, prefix)]
                sources[growth.SRC_AUTOCOMPLETE] = completions
            else:
                sources[growth.SRC_AMAZON] = await spapi_ads.fetch_product_target_suggestions(client, card["asins"])
            held = await spapi_ads.fetch_group_targeting(client, group_id, card["ad_product"])
    except AdsNotConfigured as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except AdsError as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)

    negatives = {name.upper() if growth.is_asin(name) else name
                 for _g, _t, name in await search_store.load_negatives(db, {group_id})}
    merged = growth.merge_candidates(kind, sources)
    kept, dropped = growth.exclude_existing(merged, kind=kind, held_keywords=held["keywords"],
                                            held_asins=held["asins"], negatives=negatives)
    kept = kept[:MAX_KEYWORD_CANDIDATES if kind == growth.KIND_KEYWORD else MAX_PRODUCT_CANDIDATES]
    own = {a.upper() for a in card["asins"]}
    kept = [c for c in kept if c["term"] not in own]

    titles = {}
    if kind == growth.KIND_PRODUCT and kept:
        titles = {a: p.get("title") or "" for a, p in (await pages.load_pages(db, [c["term"] for c in kept])).items()}
    candidates = [{"term": c["term"], "title": titles.get(c["term"], "")} for c in kept]
    try:
        judged = await growth_judge.judge(
            db, key=growth_judge.product_key(list(representatives)), kind=kind, page_text=page_text,
            candidates=candidates, group_label=f"{card['campaign_name']} › {card['ad_group_name']}",
            username=getattr(grant, "username", None))
    except growth_judge.JudgeError as exc:
        return JSONResponse({"error": str(exc), "judge_failed": True}, status_code=502)

    for idea in judged["ideas"]:
        if all(c["term"] != idea["term"] for c in kept):
            kept.append({"term": idea["term"], "sources": [growth.SRC_PAGE], "orders": 0, "clicks": 0,
                         "spend": 0.0, "sales": 0.0, "amazon_rank": None, "themes": []})
    kept, more = growth.exclude_existing(kept, kind=kind, held_keywords=held["keywords"],
                                         held_asins=held["asins"], negatives=negatives)
    for k, v in more.items():
        dropped[k] += v

    keep_rows = []
    for c in kept:
        verdict = judged["verdicts"].get(c["term"], (False, "not judged"))
        c["keep"], c["reason"] = verdict
        c["key"] = growth.key_of(card["ad_product"], group_id, kind, c["term"])
        c["title"] = titles.get(c["term"], "")
        if c["keep"]:
            keep_rows.append(c)

    bids: dict[str, dict] = {}
    if kind == growth.KIND_KEYWORD and card["ad_product"] == "sp" and keep_rows:
        async with httpx.AsyncClient(timeout=settings.ads_timeout) as client:
            bids = await spapi_ads.fetch_bid_recommendations(client, [
                {"entity_id": c["key"], "ad_product": "sp", "ad_group_id": group_id,
                 "campaign_id": card["campaign_id"], "match_type": "EXACT", "text": c["term"]}
                for c in keep_rows])

    now = datetime.utcnow()
    await db.execute(delete(GrowthSuggestion).where(GrowthSuggestion.ad_group_id == group_id))
    for c in kept:
        db.add(GrowthSuggestion(
            ad_group_id=group_id, key=c["key"], ad_product=card["ad_product"], campaign_id=card["campaign_id"],
            kind=kind, term=c["term"], sources_json=json.dumps(c["sources"]),
            figures_json=json.dumps({k: c.get(k) for k in ("orders", "clicks", "spend", "sales", "amazon_rank",
                                                           "themes", "title", "missing_matches")}),
            keep=bool(c["keep"]), reason=(c["reason"] or "")[:300],
            bids_json=json.dumps({k: (bids.get(c["key"]) or {}).get(k) for k in ("low", "suggested_bid", "high",
                                                                                  "unavailable")}),
            found_at=now))
    await db.commit()
    stored = (await _stored(db, [group_id])).get(group_id, [])
    return {"ad_group_id": group_id, "window": [start, end], "suggestions": stored,
            "dropped": dropped, "held_states": held["states"], "judged": judged["judged"],
            "cached": judged["cached"]}


@router.post("/add")
async def add(request: Request, db: AsyncSession = Depends(get_db),
              grant=Depends(require_area(permissions.ADS))):
    """Add the ticked suggestions to their ad group. **Writes to Amazon.**"""
    body = await request.json()
    group_id = str(body.get("ad_group_id") or "")
    keys = {str(k) for k in body.get("keys") or ()}
    mode = growth.BID_LOW if body.get("bid_mode") == growth.BID_LOW else growth.BID_CUSTOM
    custom = body.get("custom_bid")
    stored = (await db.execute(select(GrowthSuggestion).where(GrowthSuggestion.ad_group_id == group_id))).scalars().all()
    by_key = {r.key: r for r in stored}
    unknown = [k for k in keys if k not in by_key or not by_key[k].keep]
    if not keys or unknown:
        return JSONResponse({"error": "Some ticked rows are not current suggestions for this ad group — find "
                                      "again.", "unknown": sorted(unknown)[:20]}, status_code=409)
    selected = []
    for key in sorted(keys):
        row = by_key[key]
        figures = json.loads(row.figures_json or "{}")
        selected.append({"key": key, "term": row.term, "kind": row.kind, "ad_product": row.ad_product,
                         "campaign_id": row.campaign_id or "", "ad_group_id": group_id,
                         "missing_matches": figures.get("missing_matches") or [],
                         "bids": json.loads(row.bids_json or "{}")})
    guardrails = await repository.load_guardrails(db)
    rows, refused = growth.rows_to_send(selected, mode=mode, custom=custom, guardrails=guardrails)
    limit = int(guardrails.get("max_rows") or 0)
    if limit and len(rows) > limit:
        return JSONResponse({"error": f"{len(rows)} rows to add, over the {limit}-row limit."}, status_code=400)
    if not rows:
        return JSONResponse({"error": "Nothing can be sent: " + "; ".join(sorted({r["reason_text"] for r in refused})),
                             "refused": refused}, status_code=400)
    settings = get_settings()
    if not settings.ads_configured:
        return JSONResponse({"error": "Advertising credentials are not configured."}, status_code=400)

    progress.track(body.get("progress_id"))
    progress.phase("check", 1)
    try:
        async with httpx.AsyncClient(timeout=settings.ads_timeout) as client:
            ad_product = selected[0]["ad_product"]
            held = await spapi_ads.fetch_group_targeting(client, group_id, ad_product)
            progress.tick(1)
            held_asins = {a.upper() for a in held["asins"]}
            to_send = [r for r in rows if not (
                (r["kind"] == growth.KIND_PRODUCT and r["text"].upper() in held_asins)
                or (r["kind"] == growth.KIND_KEYWORD and (r["text"], r["match_type"]) in held["keywords"]))]
            already = [{"term": r["text"], "match_type": r["match_type"]} for r in rows if r not in to_send]
            if not to_send:
                return {"run_id": None, "applied": 0, "failed": 0, "pending": 0, "already_there": already,
                        "refused": refused, "results": []}
            run_id = await repository.open_run(db, to_send, rule_summary=f"Keyword growth: add {len(to_send)} "
                                               f"target(s) to an ad group")
            progress.phase("send", len(to_send))
            results: list[dict] = []
            for writer in (logic.WRITER_KEYWORD, logic.WRITER_TARGET, logic.WRITER_SB_KEYWORD, logic.WRITER_SB_TARGET):
                batch = [r for r in to_send if r["writer"] == writer]
                if batch:
                    results.extend(await spapi_ads.create_targeting(client, batch, writer=writer))
        progress.phase("record", 1)
        counts = await repository.record_create_results(db, run_id, [{**r, "ad_id": r.get("new_id")} for r in results])
        made = {r["entity_id"] for r in results if r.get("ok") and r.get("new_id")}
        added_keys = {r["key"] for r in to_send if r["entity_id"] in made}
        if added_keys:
            await db.execute(delete(GrowthSuggestion).where(GrowthSuggestion.ad_group_id == group_id,
                                                            GrowthSuggestion.key.in_(list(added_keys))))
            await db.commit()
    except AdsNotConfigured as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except AdsError as exc:
        logger.warning("ads growth add failed: %s", exc)
        return JSONResponse({"error": str(exc)}, status_code=502)
    finally:
        progress.finish()

    by_id = {r["entity_id"]: r for r in to_send}
    return {"run_id": run_id, **counts, "already_there": already, "refused": refused,
            "results": [{**r, "term": by_id.get(r["entity_id"], {}).get("text", ""),
                         "match_type": by_id.get(r["entity_id"], {}).get("match_type", "")} for r in results]}
