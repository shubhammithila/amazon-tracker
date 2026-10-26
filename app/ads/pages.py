"""Our own product page text — title, bullets, description — from the Catalog API, cached 7 days.

What Keyword growth's relevance check reads (`growth_judge`). The scraper stores only the title; the
Catalog API (`GET /catalog/2022-04-01/items/{asin}`, `includedData=attributes,summaries`) returns all three
for our own ASINs and, measured 26 Oct 2026, the same for a competitor's title. A failed read keeps an
older stored copy rather than leaving the product with nothing to be judged against.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta
from collections.abc import Awaitable, Callable, Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ProductPage

logger = logging.getLogger(__name__)

MAX_AGE_DAYS = 7
_TAGS = re.compile(r"<[^>]+>")
_SPACE = re.compile(r"\s+")


def _clean(text) -> str:
    return _SPACE.sub(" ", _TAGS.sub(" ", str(text or ""))).strip()


def parse_catalog(payload: dict) -> dict:
    """`{"title", "bullets": [str], "description"}` from a Catalog item. Pure; HTML tags stripped."""
    attrs = (payload or {}).get("attributes") or {}
    summaries = (payload or {}).get("summaries") or [{}]
    title = _clean((summaries[0] or {}).get("itemName")) or _clean(
        ((attrs.get("item_name") or [{}])[0] or {}).get("value"))
    bullets = [_clean(b.get("value")) for b in attrs.get("bullet_point") or [] if _clean(b.get("value"))]
    description = _clean(((attrs.get("product_description") or [{}])[0] or {}).get("value"))
    return {"title": title, "bullets": bullets, "description": description}


async def fetch_catalog(asin: str) -> dict:
    """One live Catalog read, parsed."""
    from app.config import get_settings
    from app.shipment import spapi

    payload = await spapi._get(f"/catalog/2022-04-01/items/{asin}", {
        "marketplaceIds": get_settings().sp_api_marketplace_id, "includedData": "attributes,summaries"})
    return parse_catalog(payload)


def _row_to_page(row: ProductPage) -> dict:
    return {"title": row.title or "", "bullets": json.loads(row.bullets_json or "[]"),
            "description": row.description or "", "fetched_at": row.fetched_at}


async def load_pages(db: AsyncSession, asins: Iterable[str], *,
                     fetch: Callable[[str], Awaitable[dict]] | None = None,
                     max_age_days: int = MAX_AGE_DAYS, now: datetime | None = None) -> dict[str, dict]:
    """`{asin: page}` for every ASIN we hold or could read. Stale (or missing) rows are re-read; a failed
    read keeps the older row, or leaves the ASIN out when there is none."""
    fetch = fetch or fetch_catalog
    now = now or datetime.utcnow()
    wanted = sorted({str(a).upper() for a in asins if a})
    rows = {r.asin: r for r in (await db.execute(
        select(ProductPage).where(ProductPage.asin.in_(wanted)))).scalars().all()} if wanted else {}
    out: dict[str, dict] = {}
    for asin in wanted:
        row = rows.get(asin)
        if row is not None and row.fetched_at and now - row.fetched_at < timedelta(days=max_age_days):
            out[asin] = _row_to_page(row)
            continue
        try:
            page = await fetch(asin)
        except Exception as exc:  # noqa: BLE001 - an old copy beats nothing
            logger.warning("ads growth: catalog read for %s failed: %s", asin, exc)
            if row is not None:
                out[asin] = _row_to_page(row)
            continue
        if not page.get("title"):
            if row is not None:
                out[asin] = _row_to_page(row)
            continue
        if row is None:
            row = ProductPage(asin=asin)
            db.add(row)
        row.title, row.description = page["title"], page.get("description") or ""
        row.bullets_json = json.dumps(page.get("bullets") or [], ensure_ascii=False)
        row.fetched_at = now
        out[asin] = {**page, "fetched_at": now}
    await db.commit()
    return out


def page_text(page: dict, *, limit: int = 3500) -> str:
    """The page as one block of text for the judge, capped so a long description cannot crowd out the
    candidates."""
    parts = [f"Title: {page.get('title') or ''}"]
    parts += [f"- {b}" for b in page.get("bullets") or []]
    if page.get("description"):
        parts.append(f"Description: {page['description']}")
    return "\n".join(parts)[:limit]
