"""Compare one calendar month of stored lines with Amazon's own Brand Analytics repeat report.

Run on the server after the backfill:  venv/bin/python scripts/validate_repeat.py 2026-09

Brand Analytics counts ALL channels; this app sees FBA only. So on ASINs that ship almost entirely
by FBA (ours >= 95% of Amazon's customer count, and >= 100 customers):
  * unique customers must agree within 3%                      -> proves the customer key
  * our repeat % must not EXCEED Amazon's by more than 0.5 pt   -> Easy Ship repeats can only be
                                                                   missing, never extra
Measured 06 Oct 2026 on the prototype: 914/925, 737/747, 223/223 customers; ours 0.3-0.6 pt lower.
Exit 1 on any failure, or when no ASIN was comparable.
"""
import asyncio
import calendar
import collections
import gzip
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import httpx  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.database import async_session  # noqa: E402
from app.repeat import repository  # noqa: E402
from app.shipment import spapi  # noqa: E402

R = "/reports/2021-06-30"


async def brand_analytics(month_start: date, month_end: date) -> dict:
    async with httpx.AsyncClient(timeout=120) as cl:
        created = await spapi._post(f"{R}/reports", {
            "reportType": "GET_BRAND_ANALYTICS_REPEAT_PURCHASE_REPORT",
            "marketplaceIds": [get_settings().sp_api_marketplace_id],
            "reportOptions": {"reportPeriod": "MONTH"},
            "dataStartTime": f"{month_start}T00:00:00Z",
            "dataEndTime": f"{month_end}T23:59:59Z"}, client=cl)
        st = {}
        for _ in range(120):
            await asyncio.sleep(15)
            st = await spapi._get(f"{R}/reports/{created['reportId']}", client=cl)
            if st["processingStatus"] in ("DONE", "FATAL", "CANCELLED"):
                break
        if st.get("processingStatus") != "DONE":
            raise SystemExit(f"Brand Analytics report {st.get('processingStatus')}")
        doc = await spapi._get(f"{R}/documents/{st['reportDocumentId']}", client=cl)
        raw = (await cl.get(doc["url"])).content
        if doc.get("compressionAlgorithm") == "GZIP":
            raw = gzip.decompress(raw)
        return {x["asin"]: x for x in json.loads(raw)["dataByAsin"]}


async def main(month: str) -> int:
    y, m = map(int, month.split("-"))
    start, end = date(y, m, 1), date(y, m, calendar.monthrange(y, m)[1])
    async with async_session() as db:
        lines = [l for l in await repository.load_lines(db, start.isoformat()) if l["day"] <= end]
    orders = collections.defaultdict(lambda: collections.defaultdict(set))   # asin -> buyer -> orders
    for l in lines:
        orders[l["child_asin"]][l["buyer_key"]].add(l["amazon_order_id"])
    ba = await brand_analytics(start, end)
    failures = checked = 0
    print(f"{'asin':12} {'ours':>6} {'Amazon':>7} {'ours rep':>9} {'Amazon rep':>11}")
    for asin, buyers in sorted(orders.items(), key=lambda kv: -len(kv[1])):
        x = ba.get(asin) or {}
        theirs = x.get("uniqueCustomers") or 0
        ours = len(buyers)
        if theirs < 100 or ours < 0.95 * theirs:
            continue                       # a meaningful Easy Ship share: not comparable
        checked += 1
        ours_rep = sum(1 for o in buyers.values() if len(o) > 1) / ours
        their_rep = float(x.get("repeatCustomersPctTotal") or 0)
        bad = abs(ours - theirs) / theirs > 0.03 or ours_rep > their_rep + 0.005
        failures += bad
        print(f"{asin:12} {ours:6d} {theirs:7d} {ours_rep:9.1%} {their_rep:11.1%}  "
              f"{'FAIL' if bad else 'ok'}")
    print(f"checked {checked} FBA-dominant ASINs, {failures} failed")
    return 1 if failures or not checked else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(sys.argv[1])))
