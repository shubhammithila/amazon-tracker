"""Page Amazon's FBA Inventory API. Read-only: one GET per page and no report queue.

Measured 13 Oct 2026: 8 pages, 354 SKUs (242 FBA), a few seconds.
"""
from __future__ import annotations

from app.config import get_settings

PATH = "/fba/inventory/v1/summaries"
MAX_PAGES = 30


class InventoryFetchError(RuntimeError):
    pass


async def fetch_summaries(get=None) -> list[dict]:
    if get is None:
        from app.shipment.spapi import _get as get
    market = get_settings().sp_api_marketplace_id
    out, token = [], None
    for _ in range(MAX_PAGES):
        params = {"details": "true", "granularityType": "Marketplace", "granularityId": market,
                  "marketplaceIds": market}
        if token:
            params["nextToken"] = token
        body = await get(PATH, params)
        out += (body.get("payload") or {}).get("inventorySummaries") or []
        token = (body.get("pagination") or {}).get("nextToken")
        if not token:
            return out
    # A cap that is hit is a truncated answer, and a truncated answer reads as missing stock.
    raise InventoryFetchError(f"Amazon returned more than {MAX_PAGES} pages of inventory")
