"""The `spTargeting` performance report — spend and sales per keyword and per target.

**These rows are the working set for every bid rule**, and that is the fact that makes the Ads tab
possible at all. This account holds 148,291 keywords and 200,000+ targeting clauses (measured; over
9 minutes to page in full), but a 7-day report returns **12,854 rows** — only entities with activity
in the window. A bid rule can only act on something that has spend or impressions, so the report is
the population a rule considers and the entity API is touched only for the rows it matched.

A different report from `portfolio.ads`'s: that one is `spAdvertisedProduct`, keyed on the ASIN being
advertised, and answers "does this product's advertising pay for itself". This one is keyed on the
KEYWORD or TARGET doing the advertising, which is the only grain a bid can be attached to. Both are
Sponsored Products, both go through the same async submit/poll/download, and they share the token
cache and the 31-day window chunking rather than reimplementing either.

Two measured properties worth stating up front:

* **~5.5 minutes to generate** a 7-day report (measured twice). Slower than the economics query's
  30 seconds and faster than `spAdvertisedProduct`'s ~12 minutes, so this is a background job with
  a progress bar, never something a page load waits on.
* **`matchType` is the routing key.** The report labels both keyword ids and target ids `keywordId`,
  and `matchType` is the only column that distinguishes them. Dropping it would make every row
  unroutable — see `ads.logic.writer_for`.
"""
from __future__ import annotations

import gzip
import json
import logging
import re

import httpx

from app.config import get_settings
from app.portfolio.ads import (
    AdsError,
    AdsNotConfigured,
    CREATE_CONTENT_TYPE,
    POLL_INTERVAL,
    POLL_MAX,
    REPORT_PATH,
    _access_token,
    _headers,
    split_window,
)

logger = logging.getLogger(__name__)

#: Keyed on the targeting expression or keyword — the grain a bid attaches to.
REPORT_TYPE_ID = "spTargeting"

AD_PRODUCT = "SPONSORED_PRODUCTS"

#: Sponsored Brands' equivalent. A different report type AND a different ad product, and its column
#: names differ too: SB reports plain `sales`/`purchases` where SP reports `sales7d`/`purchases7d`.
SB_REPORT_TYPE_ID = "sbTargeting"
SB_AD_PRODUCT = "SPONSORED_BRANDS"

#: SB's columns. Deliberately a separate tuple rather than a filtered copy of `REPORT_COLUMNS`: a bad
#: column name fails the whole request, and the two products genuinely disagree about these names.
SB_REPORT_COLUMNS = (
    "impressions", "clicks", "cost",
    "sales", "purchases",
    "keywordId", "keywordText", "matchType", "keywordBid",
    "campaignId", "campaignName", "adGroupId", "adGroupName",
)

#: **`sbTargeting` throttles and then succeeds on retry** — measured: three immediate creates all
#: returned 429, one create after a 60-second pause returned 200. `sbCampaigns` was accepted first
#: time, so the limit is per report type rather than per account. A 429 is therefore a WAIT, not a
#: failure; treating it as one would make the SB refresh fail most of the time.
THROTTLE_ATTEMPTS = 6
THROTTLE_BACKOFF = 30.0

#: `groupBy: ["targeting"]` is what makes this per-target rather than per-campaign. Verified
#: accepted; the alternatives answer a different question.
GROUP_BY = ["targeting"]

#: The columns a bid rule needs, and no more — a wider selection is slower to generate and a bad
#: column name fails the whole request with a message that lists the legal values.
#:
#: `keywordBid` is the current bid AS AT the report, and it is deliberately NOT trusted for a write:
#: `spapi_ads.fetch_current_bids` re-reads the live value before applying, because a bid edited in
#: Seller Central since the report would otherwise be overwritten with a percentage of a stale
#: number.
REPORT_COLUMNS = (
    "impressions", "clicks", "cost",
    "purchases7d", "sales7d",
    "keyword", "keywordId", "keywordType", "matchType", "targeting",
    "keywordBid",
    "campaignId", "campaignName", "adGroupId", "adGroupName",
)


def build_report_request(start: str, end: str, *, daily: bool = False,
                         ad_product: str = "sp") -> dict:
    """The report body Amazon accepts. ONE function, so the shape is stated once.

    The same three traps `portfolio.ads` documents apply here and were re-verified for this report
    type: `groupBy` must be a legal value, `date` is not a column under `timeUnit: SUMMARY`, and the
    create call needs the versioned `vnd.createasyncreportrequest.v3+json` content type.

    **`daily=True` adds the `date` column and switches to `timeUnit: DAILY`**, which is what makes
    any sub-range instant: per-day rows can be summed locally, so "I have 30 days, show me 20" needs
    no new report. Measured on a 7-day window: DAILY returns 45,650 rows against SUMMARY's 12,854.

    Note the asymmetry, which is Amazon's rather than ours: `date` is ILLEGAL under SUMMARY (it fails
    the whole request) and REQUIRED under DAILY to be useful. One flag, two column lists.
    """
    is_sb = (ad_product or "sp").strip().lower() == "sb"
    columns = list(SB_REPORT_COLUMNS if is_sb else REPORT_COLUMNS)
    if daily:
        columns.append("date")

    return {
        "name": f"ads {'sb ' if is_sb else ''}targeting {'daily ' if daily else ''}{start}..{end}",
        "startDate": start,
        "endDate": end,
        "configuration": {
            "adProduct": SB_AD_PRODUCT if is_sb else AD_PRODUCT,
            "groupBy": list(GROUP_BY),
            "columns": columns,
            "reportTypeId": SB_REPORT_TYPE_ID if is_sb else REPORT_TYPE_ID,
            "timeUnit": "DAILY" if daily else "SUMMARY",
            "format": "GZIP_JSON",
        },
    }


def aggregate(rows: list[dict]) -> list[dict]:
    """Sum raw report rows to ONE row per entity id.

    Needed for two independent reasons, and either alone would justify it:

    * **A window over 31 days is several reports** (Amazon's per-report cap), so the same keyword
      appears once per chunk and its spend must be added rather than overwritten.
    * `spAdvertisedProduct` arrives split by campaign even when not asked; this report has not been
      observed to split, but collapsing defensively costs nothing and means two grains can never
      reach the rule engine.

    Sums counts and money; keeps the LAST non-null bid and the descriptive fields, since those
    describe the entity rather than the window.
    """
    merged: dict[str, dict] = {}
    for row in rows:
        identifier = str(row.get("keywordId") or "")
        if not identifier:
            continue
        existing = merged.get(identifier)
        if existing is None:
            merged[identifier] = dict(row)
            continue
        # BOTH products' column names: SP reports `sales7d`/`purchases7d`, SB reports plain
        # `sales`/`purchases`. Summing only SP's names would silently discard SB sales when a
        # window is chunked, leaving an SB ROAS of zero that looks like a real result.
        for field in ("impressions", "clicks", "purchases7d", "purchases"):
            if field in existing or field in row:
                existing[field] = (existing.get(field) or 0) + (row.get(field) or 0)
        for field in ("cost", "sales7d", "sales"):
            if field in existing or field in row:
                existing[field] = round(float(existing.get(field) or 0)
                                        + float(row.get(field) or 0), 2)
        # A later chunk's bid is the more recent one.
        if row.get("keywordBid") is not None:
            existing["keywordBid"] = row["keywordBid"]
    return list(merged.values())


async def fetch_targeting(
    start: str,
    end: str,
    *,
    daily: bool = False,
    ad_product: str = "sp",
    sleep=None,
    on_progress=None,
) -> list[dict]:
    """Run the targeting report for a window and return aggregated rows.

    A window longer than 31 days becomes several reports, summed — `split_window` is shared with
    `portfolio.ads` rather than reimplemented, because that cap was learned the hard way there and
    one copy cannot drift from the other.

    **A failed chunk fails the whole window.** Two chunks of spend against three chunks of sales is
    not a smaller ROAS, it is a wrong one — and here it would drive a bid change, so a partial
    answer is worse than none.
    """
    import asyncio

    sleep = sleep or asyncio.sleep
    settings = get_settings()
    if not settings.ads_configured:
        raise AdsNotConfigured()

    chunks = split_window(start, end)
    raw: list[dict] = []

    async with httpx.AsyncClient(timeout=settings.ads_timeout) as client:
        for index, (chunk_start, chunk_end) in enumerate(chunks):
            if len(chunks) > 1:
                logger.info(
                    "ads: targeting report chunk %d/%d (%s..%s)",
                    index + 1, len(chunks), chunk_start, chunk_end,
                )

            def chunk_progress(done, total, _i=index):
                # One bar across all chunks: three reports must read 0->100% once rather than
                # snapping back to zero twice, the same rule the orders bar follows.
                if on_progress:
                    on_progress(_i * total + done, total * len(chunks))

            raw.extend(await _one_report(
                client, chunk_start, chunk_end, daily=daily, ad_product=ad_product,
                sleep=sleep, on_progress=chunk_progress,
            ))

    # **Daily rows are NOT aggregated.** Collapsing them to one row per entity is exactly what this
    # mode exists to avoid — the per-day grain is the whole point, and `aggregate` keys on entity id
    # alone so it would silently discard 29 days out of 30.
    rows = raw if daily else aggregate(raw)
    logger.info(
        "ads: %d targeting report(s) for %s..%s -> %d raw row(s) aggregated to %d entity row(s)",
        len(chunks), start, end, len(raw), len(rows),
    )
    return rows


async def _one_report(client, start: str, end: str, *, daily: bool = False,
                      ad_product: str = "sp", sleep, on_progress=None) -> list[dict]:
    """Create, poll and download ONE report. Returns RAW rows, un-aggregated.

    Raw so `fetch_targeting` sums every chunk through the single `aggregate` path — aggregating per
    chunk and merging afterwards would be two summing rules for one grain.
    """
    settings = get_settings()
    token = await _access_token(client)
    head = _headers(token)

    body = json.dumps(build_report_request(start, end, daily=daily, ad_product=ad_product))

    # **A 429 here is a WAIT, not a failure.** Measured on `sbTargeting`: three immediate creates all
    # returned 429 and one after a 60-second pause returned 200, while `sbCampaigns` was accepted
    # first time — so the limit is per report type. Treating 429 as an error would make the SB
    # refresh fail most of the time it is asked to run.
    for attempt in range(THROTTLE_ATTEMPTS):
        create = await client.post(
            settings.ads_endpoint + REPORT_PATH,
            content=body,
            headers={**head, "Content-Type": CREATE_CONTENT_TYPE},
        )
        if create.status_code != 429:
            break
        wait = THROTTLE_BACKOFF * (attempt + 1)
        logger.info(
            "ads: report creation throttled (attempt %d/%d), waiting %.0fs",
            attempt + 1, THROTTLE_ATTEMPTS, wait,
        )
        await sleep(wait)

    # **425 means "duplicate of <reportId>" and is a SUCCESS in disguise.** Measured while capturing
    # a fixture: after two 429s, the third create returned
    #     425 {"code":"425","detail":"The Request is a duplicate of : 9553f2aa-..."}
    # Amazon had accepted an identical request during one of the throttled attempts and deduplicated
    # this one. That report existed and completed normally — so treating 425 as a failure would throw
    # away a report we had already paid for, and the retry above makes hitting it LIKELY rather than
    # rare. The id is recovered from the message because Amazon does not return it as a field.
    duplicate_id = None
    if create.status_code == 425:
        match = re.search(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
                          create.text or "")
        duplicate_id = match.group(1) if match else None
        if duplicate_id:
            logger.info(
                "ads: report request deduplicated by Amazon, following its existing report %s",
                duplicate_id,
            )

    if create.status_code >= 400 and not duplicate_id:
        # Amazon's validation messages name the cause — they are how the 31-day cap and the legal
        # `groupBy` values were both found — so they are surfaced verbatim.
        raise AdsError(
            f"Amazon refused the targeting report request: {create.text[:300]}",
            status=create.status_code,
        )
    report_id = duplicate_id or (create.json() or {}).get("reportId")
    if not report_id:
        raise AdsError(
            f"Amazon accepted the report but returned no reportId: {create.text[:200]}"
        )
    logger.info("ads: targeting report %s requested for %s..%s", report_id, start, end)

    url = None
    for attempt in range(POLL_MAX):
        await sleep(POLL_INTERVAL)
        status = await client.get(
            f"{settings.ads_endpoint}{REPORT_PATH}/{report_id}", headers=head
        )
        if status.status_code >= 400:
            raise AdsError(
                f"Polling the targeting report failed: {status.text[:200]}",
                status=status.status_code,
            )
        state = status.json() or {}
        if on_progress:
            on_progress(attempt + 1, POLL_MAX)
        processing = state.get("status")
        if processing not in ("COMPLETED", "FAILURE", "CANCELLED"):
            continue
        if processing != "COMPLETED":
            raise AdsError(
                f"Amazon could not produce the targeting report ({processing})"
                + (f": {state.get('failureReason')}" if state.get("failureReason") else "")
            )
        url = state.get("url")
        # COMPLETED with no url means no advertising activity in the window. An empty list is the
        # right answer, and it is NOT how failure is reported — every failure raises.
        if not url:
            logger.info("ads: targeting report %s completed with no data", report_id)
            return []
        break
    else:
        raise AdsError(
            f"The targeting report was still generating after "
            f"{int(POLL_MAX * POLL_INTERVAL / 60)} minutes. It may finish later — press Refresh "
            "to check again."
        )

    # Pre-signed url: fetched WITHOUT the ads headers, or the bearer token leaks to S3.
    download = await client.get(url)
    if download.status_code >= 400:
        raise AdsError(
            f"Downloading the targeting report failed ({download.status_code}).",
            status=download.status_code,
        )

    payload = download.content
    # GZIP_JSON was requested, but the magic bytes decide: the economics document arrived
    # UNCOMPRESSED despite the documentation, so trusting the request would repeat that mistake.
    if payload[:2] == b"\x1f\x8b":
        payload = gzip.decompress(payload)
    try:
        rows = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AdsError(f"The targeting report was not readable JSON: {exc}")
    if not isinstance(rows, list):
        raise AdsError("The targeting report was not a list of rows.")

    logger.info("ads: targeting report %s -> %d raw row(s)", report_id, len(rows))
    return rows
