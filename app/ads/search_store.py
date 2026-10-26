"""Search-term rows and the account's existing negatives: storage and reads for the negative rules.

Spec: `docs/superpowers/specs/2026-10-25-ads-search-term-negatives-design.md`. Kept beside
`repository.py` rather than inside it: these tables have their own grain (a search term within an ad
group) and their own readers, and `repository.py` is already the longest module in the app.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime

from sqlalchemy import delete, func, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ads import logic
from app.models import AdsEntity, AdsPerformanceDaily, AdsSearchTermDaily

logger = logging.getLogger(__name__)

#: `ads_entity.entity_type` for a cached negative: a negative keyword (exact) or negative product.
NEG_KEYWORD = "neg_keyword"
NEG_TARGET = "neg_target"

_ASIN_IN = re.compile(r'asin(?:-expanded)?="?([a-z0-9]{10})"?', re.I)


def _text(value, size=300) -> str | None:
    value = str(value or "").strip()
    return value[:size] or None


async def save_search_term_daily(db: AsyncSession, rows: list[dict], *, ad_product: str) -> int:
    """Store DAILY search-term rows for one ad product, delete-then-insert per day (like `save_daily`).

    SP names its figures `sales7d`/`purchases7d` and its keyword `keyword`; SB says `sales`,
    `purchases` and `keywordText`. Rows with no day or no search term are skipped, never guessed.
    """
    product = (ad_product or "sp").lower()
    now = datetime.utcnow()
    values = []
    for raw in rows or ():
        day = str(raw.get("date") or "")[:10]
        term = _text(raw.get("searchTerm"))
        if not day or not term:
            continue
        values.append({
            "day": day, "ad_product": product,
            "campaign_id": _text(raw.get("campaignId"), 32),
            "campaign_name": _text(raw.get("campaignName")),
            "ad_group_id": _text(raw.get("adGroupId"), 32),
            "ad_group_name": _text(raw.get("adGroupName")),
            "keyword_id": _text(raw.get("keywordId"), 32),
            "keyword_text": _text(raw.get("keyword") or raw.get("keywordText")),
            "match_type": _text(raw.get("matchType"), 40),
            "targeting": _text(raw.get("targeting")),
            "search_term": term.lower(),
            "impressions": logic._as_int(raw.get("impressions")),
            "clicks": logic._as_int(raw.get("clicks")),
            "cost": round(logic._as_float(raw.get("cost")), 2),
            "orders": logic._as_int(raw.get("purchases7d", raw.get("purchases"))),
            "sales": round(logic._as_float(raw.get("sales7d", raw.get("sales"))), 2),
            "fetched_at": now,
        })
    if not values:
        return 0
    days = sorted({v["day"] for v in values})
    await db.execute(delete(AdsSearchTermDaily).where(
        AdsSearchTermDaily.day.in_(days), AdsSearchTermDaily.ad_product == product))
    for start in range(0, len(values), 5000):
        await db.execute(insert(AdsSearchTermDaily), values[start:start + 5000])
    await db.commit()
    logger.info("ads: stored %d %s search-term row(s) across %d day(s)", len(values), product, len(days))
    return len(values)


async def search_days_held(db: AsyncSession) -> dict[str, set[str]]:
    """`{ad_product: {day}}` for the search-term table."""
    out: dict[str, set[str]] = {}
    for product, day in (await db.execute(
            select(AdsSearchTermDaily.ad_product, AdsSearchTermDaily.day)
            .group_by(AdsSearchTermDaily.ad_product, AdsSearchTermDaily.day))).all():
        out.setdefault(product, set()).add(day)
    return out


async def sum_search_terms(db: AsyncSession, start: str, end: str, *, campaign_ids=None,
                           ad_group_ids=None) -> list[dict]:
    """One row per (ad product, ad group, search term) over the window, with the targets that
    brought it in (`via`). No Amazon call."""
    query = (
        select(
            AdsSearchTermDaily.ad_product, AdsSearchTermDaily.campaign_id,
            AdsSearchTermDaily.ad_group_id, AdsSearchTermDaily.search_term,
            AdsSearchTermDaily.keyword_id, AdsSearchTermDaily.keyword_text,
            AdsSearchTermDaily.match_type, AdsSearchTermDaily.targeting,
            func.max(AdsSearchTermDaily.campaign_name), func.max(AdsSearchTermDaily.ad_group_name),
            func.sum(AdsSearchTermDaily.impressions), func.sum(AdsSearchTermDaily.clicks),
            func.sum(AdsSearchTermDaily.cost), func.sum(AdsSearchTermDaily.orders),
            func.sum(AdsSearchTermDaily.sales),
        )
        .where(AdsSearchTermDaily.day >= start, AdsSearchTermDaily.day <= end)
        .group_by(AdsSearchTermDaily.ad_product, AdsSearchTermDaily.campaign_id,
                  AdsSearchTermDaily.ad_group_id, AdsSearchTermDaily.search_term,
                  AdsSearchTermDaily.keyword_id, AdsSearchTermDaily.keyword_text,
                  AdsSearchTermDaily.match_type, AdsSearchTermDaily.targeting)
    )
    if campaign_ids:
        query = query.where(AdsSearchTermDaily.campaign_id.in_([str(c) for c in campaign_ids]))
    if ad_group_ids:
        query = query.where(AdsSearchTermDaily.ad_group_id.in_([str(a) for a in ad_group_ids]))
    terms: dict[tuple, dict] = {}
    for (product, campaign, group, term, kid, ktext, match, targeting, cname, gname,
         imps, clicks, cost, orders, sales) in (await db.execute(query)).all():
        row = terms.setdefault((product, campaign, group, term), {
            "ad_product": product, "campaign_id": campaign or "", "ad_group_id": group or "",
            "campaign_name": cname or "", "ad_group_name": gname or "", "search_term": term,
            "impressions": 0, "clicks": 0, "spend": 0.0, "orders": 0, "sales": 0.0, "via": []})
        row["impressions"] += int(imps or 0)
        row["clicks"] += int(clicks or 0)
        row["orders"] += int(orders or 0)
        row["spend"] = round(row["spend"] + float(cost or 0), 2)
        row["sales"] = round(row["sales"] + float(sales or 0), 2)
        row["via"].append({"keyword_id": kid or "", "text": ktext or "", "match_type": match or "",
                           "targeting": targeting or "", "spend": round(float(cost or 0), 2)})
        row["campaign_name"] = row["campaign_name"] or cname or ""
        row["ad_group_name"] = row["ad_group_name"] or gname or ""
    out = list(terms.values())
    out.sort(key=lambda r: -r["spend"])
    return out


async def search_ad_groups(db: AsyncSession) -> set[str]:
    """Every ad group that has a stored search term — the groups whose negatives matter."""
    return {str(g) for (g,) in (await db.execute(
        select(AdsSearchTermDaily.ad_group_id).where(AdsSearchTermDaily.ad_group_id.is_not(None))
        .distinct())).all()}


async def group_positives(db: AsyncSession, ad_group_ids, start: str, end: str) -> dict[str, set[str]]:
    """`{ad_group_id: {lower-case keyword text or ASIN}}` — the group's OWN keywords and product
    targets seen in the window (targeting report and search-term report), for the term-is-the-target
    guard."""
    groups = sorted({str(g) for g in ad_group_ids if g})
    out: dict[str, set[str]] = {g: set() for g in groups}

    def add(group, text, targeting=""):
        if not group or group not in out:
            return
        for value in (text, targeting):
            value = str(value or "").strip().lower()
            if not value:
                continue
            match = _ASIN_IN.search(value)
            out[group].add(match.group(1).lower() if match else value)

    for start_i in range(0, len(groups), 500):
        chunk = groups[start_i:start_i + 500]
        for group, text in (await db.execute(
                select(AdsPerformanceDaily.ad_group_id, AdsPerformanceDaily.text)
                .where(AdsPerformanceDaily.ad_group_id.in_(chunk),
                       AdsPerformanceDaily.day >= start, AdsPerformanceDaily.day <= end)
                .distinct())).all():
            add(group, text)
        for group, text, targeting in (await db.execute(
                select(AdsSearchTermDaily.ad_group_id, AdsSearchTermDaily.keyword_text,
                       AdsSearchTermDaily.targeting)
                .where(AdsSearchTermDaily.ad_group_id.in_(chunk),
                       AdsSearchTermDaily.day >= start, AdsSearchTermDaily.day <= end)
                .distinct())).all():
            add(group, text, targeting)
    return out


async def replace_negatives(db: AsyncSession, rows: list[dict]) -> int:
    """Replace the cached negatives (`ads_entity` `neg_keyword` / `neg_target`) whole. **An empty
    list writes nothing**, so a failed fetch cannot make every term look un-negated."""
    rows = [r for r in rows or () if r.get("entity_id") and r.get("entity_type") in (NEG_KEYWORD, NEG_TARGET)]
    if not rows:
        return 0
    now = datetime.utcnow()
    await db.execute(delete(AdsEntity).where(AdsEntity.entity_type.in_((NEG_KEYWORD, NEG_TARGET))))
    values = [{
        "entity_type": r["entity_type"], "ad_product": r.get("ad_product") or "sp",
        "entity_id": str(r["entity_id"]), "parent_id": r.get("parent_id") or None,
        "campaign_id": r.get("campaign_id") or None, "name": (r.get("name") or "")[:500].lower(),
        "state": r.get("state") or "ENABLED", "fetched_at": now,
    } for r in rows]
    for start in range(0, len(values), 5000):
        await db.execute(insert(AdsEntity), values[start:start + 5000])
    await db.commit()
    return len(values)


async def add_negatives_to_cache(db: AsyncSession, rows: list[dict]) -> None:
    """Put negatives a run just created into the cache, so the next preview does not offer them."""
    for r in rows or ():
        db.add(AdsEntity(entity_type=r["entity_type"], ad_product=r.get("ad_product") or "sp",
                         entity_id=str(r["entity_id"]), parent_id=r.get("parent_id"),
                         campaign_id=r.get("campaign_id"), name=(r.get("name") or "").lower(),
                         state="ENABLED", fetched_at=datetime.utcnow()))
    await db.commit()


async def load_negatives(db: AsyncSession, ad_group_ids=None) -> set[tuple[str, str, str]]:
    """`{(ad_group_id, entity_type, lower-case text or ASIN)}` of ENABLED cached negatives."""
    query = select(AdsEntity.parent_id, AdsEntity.entity_type, AdsEntity.name, AdsEntity.state).where(
        AdsEntity.entity_type.in_((NEG_KEYWORD, NEG_TARGET)))
    out = set()
    for group, etype, name, state in (await db.execute(query)).all():
        if str(state or "").upper() == "ARCHIVED":
            continue
        if ad_group_ids is not None and str(group) not in ad_group_ids:
            continue
        out.add((str(group or ""), etype, str(name or "").lower()))
    return out


async def purge(db: AsyncSession, cutoff: str) -> int:
    result = await db.execute(delete(AdsSearchTermDaily).where(AdsSearchTermDaily.day < cutoff))
    await db.commit()
    return int(result.rowcount or 0)
