"""Every SKU on the account, from `GET_MERCHANT_LISTINGS_ALL_DATA` — what the SKU coverage check
compares each SP ad group against.

Measured 24 Oct 2026: 688 rows in 32 s; status Active 235 / Inactive 380 / Incomplete 73;
`fulfillment-channel` `AMAZON_IN` 348 (FBA and Flex) / `DEFAULT` 340 (Easy Ship). Tab-separated, and
the FIRST header carries a byte-order mark (`\\ufeffitem-name`), so headers are cleaned before use.

**A good read replaces the stored list whole; an empty or failed read writes nothing**, so a refused
report can never make every SKU look missing. Each attempt is recorded (when, rows, error) under
`portfolio_settings.listings_refresh` so the screen can say how old the list is and why a read failed.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime

from sqlalchemy import delete, insert, select

from app.config import get_settings
from app.models import ListingSku, PortfolioSettings
from app.repeat import fetch as report_fetch
from app.shipment import spapi

logger = logging.getLogger(__name__)

REPORT_TYPE = "GET_MERCHANT_LISTINGS_ALL_DATA"
#: Measured 32 s for the whole account; ten minutes is a ceiling, not an expectation.
POLL_INTERVAL = 15.0
POLL_MAX = 40
STATUS_SETTING = "listings_refresh"

#: Amazon refuses to advertise a listing it calls Incomplete, so the check never expects one.
INCOMPLETE = "incomplete"


class ListingsFailed(Exception):
    pass


def _clean(key) -> str:
    return str(key or "").replace("\ufeff", "").strip().lower()


def parse_rows(rows) -> list[dict]:
    """Report rows -> `{seller_sku, asin, status, fulfillment_channel, item_name}`. Pure.

    A row with no SKU is skipped; a repeated SKU keeps its first row (Amazon lists each once).
    """
    out: dict[str, dict] = {}
    for raw in rows or ():
        row = {_clean(k): (v or "").strip() for k, v in dict(raw).items()}
        sku = row.get("seller-sku") or ""
        if not sku or sku in out:
            continue
        out[sku] = {
            "seller_sku": sku[:80],
            "asin": (row.get("asin1") or "")[:10],
            "status": (row.get("status") or "")[:20],
            "fulfillment_channel": (row.get("fulfillment-channel") or "")[:20],
            "item_name": (row.get("item-name") or "")[:500],
        }
    return list(out.values())


async def fetch(client, *, sleep=asyncio.sleep) -> list[dict]:
    """Create the report, wait for it, download and parse it. Raises `ListingsFailed`."""
    body = {"reportType": REPORT_TYPE, "marketplaceIds": [get_settings().sp_api_marketplace_id]}
    created = await report_fetch._patient(
        lambda: spapi._post(f"{report_fetch.REPORTS}/reports", body, client=client), sleep)
    report_id = created["reportId"]
    for _ in range(POLL_MAX):
        status = await report_fetch._patient(
            lambda: spapi._get(f"{report_fetch.REPORTS}/reports/{report_id}", client=client), sleep)
        state = status.get("processingStatus")
        if state == "DONE":
            return parse_rows(await report_fetch._download(status["reportDocumentId"], client))
        if state in ("FATAL", "CANCELLED"):
            raise ListingsFailed(f"Amazon reported {state} for the listings report")
        await sleep(POLL_INTERVAL)
    raise ListingsFailed(f"the listings report was still not ready after {POLL_MAX} polls")


async def replace_listings(db, rows: list[dict]) -> int:
    """Replace the stored list whole. **An empty list writes nothing.**"""
    rows = [r for r in rows or () if r.get("seller_sku")]
    if not rows:
        return 0
    now = datetime.utcnow()
    await db.execute(delete(ListingSku))
    await db.execute(insert(ListingSku), [{**r, "fetched_at": now} for r in rows])
    await db.commit()
    return len(rows)


async def load_listings(db) -> list[dict]:
    rows = (await db.execute(select(ListingSku))).scalars().all()
    return [{"seller_sku": r.seller_sku, "asin": r.asin or "", "status": r.status or "",
             "fulfillment_channel": r.fulfillment_channel or "", "item_name": r.item_name or ""}
            for r in rows]


async def load_status(db) -> dict:
    """`{at, rows, error, ok_at}` of the last attempt and the last good read, or `{}`."""
    row = (await db.execute(
        select(PortfolioSettings).where(PortfolioSettings.name == STATUS_SETTING))).scalar_one_or_none()
    if not row or not row.value_json:
        return {}
    try:
        return json.loads(row.value_json) or {}
    except json.JSONDecodeError:
        return {}


async def save_status(db, *, rows: int, error: str | None) -> dict:
    now = datetime.utcnow().isoformat(timespec="seconds")
    status = await load_status(db)
    status.update({"at": now, "rows": rows, "error": error})
    if not error:
        status["ok_at"] = now
    row = (await db.execute(
        select(PortfolioSettings).where(PortfolioSettings.name == STATUS_SETTING))).scalar_one_or_none()
    if row is None:
        db.add(PortfolioSettings(name=STATUS_SETTING, value_json=json.dumps(status)))
    else:
        row.value_json = json.dumps(status)
    await db.commit()
    return status


_LOCK = asyncio.Lock()


async def refresh(db_factory, client, *, sleep=asyncio.sleep) -> dict:
    """Read the listings and store them. **Never raises**: the failure is recorded and returned.

    One at a time (a lock), because the button and the 08:00 refresh could otherwise both ask Amazon.
    """
    async with _LOCK:
        try:
            rows = await fetch(client, sleep=sleep)
            if not rows:
                raise ListingsFailed("Amazon returned an empty listings report")
            async with db_factory() as db:
                stored = await replace_listings(db, rows)
                return await save_status(db, rows=stored, error=None)
        except Exception as exc:  # noqa: BLE001 - recorded, shown on screen, never fatal
            logger.warning("ads: listings read failed: %s", exc)
            async with db_factory() as db:
                return await save_status(db, rows=0, error=f"The listings could not be read: {exc}")


#: The Ads refresh re-reads the listings when the last GOOD read is older than this. Listings change
#: when a SKU is created, which is rare, and the panel has its own "Re-read listings" button.
STALE_AFTER_HOURS = 12


async def refresh_if_stale(db_factory, client, *, now=None, sleep=asyncio.sleep) -> dict | None:
    """`refresh` when the last good read is older than `STALE_AFTER_HOURS`; else None. Never raises."""
    try:
        async with db_factory() as db:
            ok_at = (await load_status(db)).get("ok_at")
        now = now or datetime.utcnow()
        if ok_at and (now - datetime.fromisoformat(ok_at)).total_seconds() < STALE_AFTER_HOURS * 3600:
            return None
        return await refresh(db_factory, client, sleep=sleep)
    except Exception as exc:  # noqa: BLE001 - the Ads refresh must not fail for this
        logger.warning("ads: listings check failed: %s", exc)
        return None
