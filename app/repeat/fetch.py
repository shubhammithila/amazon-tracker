"""SP-API Reports: `GET_AMAZON_FULFILLED_SHIPMENTS_DATA_GENERAL`, create -> poll -> download.

Measured: a 90-day request is FATAL, 30 days works; a 30-day chunk takes 30-40 minutes because the
queue is SERIAL per report type and shared with another requester on this account. So the nightly
job asks for 7 days and the backfill walks 30-day chunks, storing each as it lands.

The pre-signed S3 download carries NO SP-API headers, on purpose: the token must not go to S3.
"""
from __future__ import annotations

import asyncio
import csv
import gzip
import io
from datetime import date, timedelta

from app import ist
from app.config import get_settings
from app.shipment import spapi

REPORT_TYPE = "GET_AMAZON_FULFILLED_SHIPMENTS_DATA_GENERAL"
REPORTS = "/reports/2021-06-30"
MAX_REPORT_DAYS = 30
POLL_INTERVAL = 30.0
#: Two hours: a 30-day chunk has been measured 40+ minutes behind other reports in the queue.
POLL_MAX = 240


class ReportFailed(Exception):
    pass


def split_days(start: date, end: date, size: int = MAX_REPORT_DAYS) -> list[tuple[date, date]]:
    """Inclusive (start, end) pairs of at most `size` days, covering every day exactly once."""
    out, cur = [], start
    while cur <= end:
        stop = min(end, cur + timedelta(days=size - 1))
        out.append((cur, stop))
        cur = stop + timedelta(days=1)
    return out


async def fetch_rows(start: date, end: date, *, client, sleep=asyncio.sleep) -> list[dict]:
    """Every row Amazon holds for the IST days start..end inclusive."""
    if (end - start).days + 1 > MAX_REPORT_DAYS:
        raise ValueError(f"{start}..{end} is over {MAX_REPORT_DAYS} days; Amazon refuses it")
    created = await spapi._post(f"{REPORTS}/reports", {
        "reportType": REPORT_TYPE,
        "marketplaceIds": [get_settings().sp_api_marketplace_id],
        "dataStartTime": ist.utc_instant(start),
        "dataEndTime": ist.utc_instant(end + timedelta(days=1)),
    }, client=client)
    report_id = created["reportId"]
    for _ in range(POLL_MAX):
        status = await spapi._get(f"{REPORTS}/reports/{report_id}", client=client)
        state = status.get("processingStatus")
        if state == "DONE":
            return await _download(status["reportDocumentId"], client)
        if state in ("FATAL", "CANCELLED"):
            raise ReportFailed(f"Amazon reported {state} for {start}..{end}")
        await sleep(POLL_INTERVAL)
    raise ReportFailed(f"report for {start}..{end} still not ready after {POLL_MAX} polls")


async def _download(document_id: str, client) -> list[dict]:
    doc = await spapi._get(f"{REPORTS}/documents/{document_id}", client=client)
    raw = (await client.get(doc["url"])).content
    if doc.get("compressionAlgorithm") == "GZIP":
        raw = gzip.decompress(raw)
    return list(csv.DictReader(io.StringIO(raw.decode("utf-8", "replace")), delimiter="\t"))
