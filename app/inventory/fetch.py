"""Page Amazon's FBA Inventory API. Read-only: one GET per page and no report queue.

Measured 13 Oct 2026: 8 pages, 354 SKUs (242 FBA), a few seconds.

**Paced, because the first production read was refused.** `getInventorySummaries` allows about 2
requests a second with a burst of 2, and eight pages sent back to back from the EC2 box came back
"You exceeded your quota for the requested resource" on page 3. The same loop had worked from a
laptop only because each round trip there was slower. So pages are spaced by `PAGE_INTERVAL`, and a
quota refusal waits and retries (`QUOTA_WAITS`) before the run is recorded as failed.
"""
from __future__ import annotations

import asyncio

from app.config import get_settings

PATH = "/fba/inventory/v1/summaries"
MAX_PAGES = 30
PAGE_INTERVAL = 0.6
QUOTA_WAITS = (2, 5, 15)


class InventoryFetchError(RuntimeError):
    pass


async def _live_get(path: str, params: dict) -> dict:
    from app.shipment.spapi import _get
    return await _get(path, params)


def _is_quota(exc: Exception) -> bool:
    return getattr(exc, "status", None) == 429 or "quota" in str(exc).lower()


async def _patient(get, params, sleep):
    for wait in QUOTA_WAITS:
        try:
            return await get(PATH, params)
        except Exception as exc:  # noqa: BLE001 - only a quota refusal is retried
            if not _is_quota(exc):
                raise
            await sleep(wait)
    return await get(PATH, params)


async def fetch_summaries(get=None, sleep=asyncio.sleep) -> list[dict]:
    # Looked up at call time, so `tests/conftest.no_live_inventory` can stop every test reaching
    # Amazon for real (the developer .env holds live SP-API credentials).
    get = get or _live_get
    market = get_settings().sp_api_marketplace_id
    out, token = [], None
    for page in range(MAX_PAGES):
        params = {"details": "true", "granularityType": "Marketplace", "granularityId": market,
                  "marketplaceIds": market}
        if token:
            params["nextToken"] = token
        if page:
            await sleep(PAGE_INTERVAL)
        body = await _patient(get, params, sleep)
        out += (body.get("payload") or {}).get("inventorySummaries") or []
        token = (body.get("pagination") or {}).get("nextToken")
        if not token:
            return out
    # A cap that is hit is a truncated answer, and a truncated answer reads as missing stock.
    raise InventoryFetchError(f"Amazon returned more than {MAX_PAGES} pages of inventory")
